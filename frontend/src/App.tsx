import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'

type Json = Record<string, any>
type View = 'tasks' | 'templates' | 'leads' | 'sources' | 'messages' | 'operations'

const labels: Record<View, [string, string]> = {
  tasks: ['我的任务', '管理正在关注的业务条件'],
  templates: ['业务模式', '配置任务字段和列表列'],
  leads: ['线索工作台', '核对匹配理由并记录处理进度'],
  sources: ['来源管理', '按账号选择允许保存的群和联系人'],
  messages: ['原文与复查', '搜索保留期内消息并修正漏判'],
  operations: ['运行与数据', '检查连接、积压、保留和备份'],
}

const NAPCAT_HOME = 'https://github.com/NapNeko/NapCatQQ'
const NAPCAT_DOWNLOAD = `${NAPCAT_HOME}/releases`

let csrf = ''

async function api(path: string, options: RequestInit = {}) {
  const method = options.method || 'GET'
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD'].includes(method)) headers.set('X-CSRF-Token', csrf)
  const response = await fetch(`/api${path}`, { ...options, headers, credentials: 'same-origin' })
  const payload = await response.json().catch(() => ({ code: 'bad_response', message: '服务器返回了无法读取的响应' }))
  if (!response.ok) throw new Error(payload.message || '操作失败')
  return payload
}

async function allPages(path: string) {
  const items: Json[] = []; let cursor = ''
  do {
    const result = await api(path + (cursor ? `${path.includes('?') ? '&' : '?'}cursor=${encodeURIComponent(cursor)}` : ''))
    items.push(...result.items); cursor = result.next_cursor || ''
  } while (cursor)
  return items
}

function formatTime(value?: string | null) {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value)) : '暂无'
}

function bytes(value = 0) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let index = 0; let size = value
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index++ }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`
}

function Dialog({ open, title, children, onClose, wide = false }: { open: boolean, title: string, children: ReactNode, onClose: () => void, wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { if (open && !ref.current?.open) ref.current?.showModal(); if (!open && ref.current?.open) ref.current.close() }, [open])
  return <dialog ref={ref} className={wide ? 'dialog wide' : 'dialog'} onClose={onClose} onClick={event => { if (event.target === ref.current) onClose() }}>
    <header><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭">×</button></header>
    {children}
  </dialog>
}

function Badge({ value }: { value: string }) {
  const text: Record<string, string> = {
    active: '运行中', paused: '已暂停', completed: '已完成', archived: '已归档',
    match: '匹配', review: '待核对', conflict: '冲突', not_match: '未命中', pending: '待处理', failed: '失败',
    new: '新线索', contacted: '已联系', ignored: '已忽略', supply: '供货', demand: '求购', unknown: '不明确',
  }
  return <span className={`badge ${value}`}>{text[value] || value}</span>
}

function Auth({ initialized, onDone }: { initialized: boolean, onDone: (token: string) => void }) {
  const [code, setCode] = useState(''); const [password, setPassword] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const result = await api(initialized ? '/auth/login' : '/auth/setup', { method: 'POST', body: JSON.stringify(initialized ? { password } : { code, password }) })
      onDone(result.csrf_token)
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }
  return <main className="auth-page"><section className="auth-card">
    <img className="auth-logo" src="/huginn-leads-logo.png" alt="Huginn Leads" /><h1>Huginn Leads</h1><p>{initialized ? '登录本地消息工作台' : '首次使用：请在启动窗口查看初始化码，并设置本地密码。'}</p>
    <form onSubmit={submit}>
      {!initialized && <label>初始化码<input autoFocus value={code} onChange={e => setCode(e.target.value)} required autoComplete="one-time-code" /></label>}
      <label>密码<input autoFocus={initialized} type="password" value={password} onChange={e => setPassword(e.target.value)} required minLength={12} autoComplete={initialized ? 'current-password' : 'new-password'} /></label>
      {error && <div className="alert error" role="alert">{error}</div>}
      <button className="primary" disabled={busy}>{busy ? '请稍候…' : initialized ? '登录' : '完成初始化'}</button>
    </form><small>仅监听本机 127.0.0.1，不会把 OneBot Token 发送到浏览器。</small>
  </section></main>
}

function FieldInput({ field, value, onChange }: { field: Json, value: any, onChange: (value: any) => void }) {
  if (field.type === 'boolean') return <label className="inline-check dynamic-check"><input type="checkbox" checked={!!value} onChange={e => onChange(e.target.checked)} />{field.label}{field.help && <small>{field.help}</small>}</label>
  if (field.type === 'select') return <label>{field.label}<select value={value ?? ''} onChange={e => onChange(e.target.value)} required={field.required}><option value="">请选择…</option>{field.options?.map((option: Json) => <option key={option.value} value={option.value}>{option.label}</option>)}</select>{field.help && <small>{field.help}</small>}</label>
  return <label>{field.label}<input type={field.type === 'number' ? 'number' : field.type === 'date' ? 'date' : 'text'} value={value ?? ''} onChange={e => onChange(e.target.value)} required={field.required} />{field.help && <small>{field.help}</small>}</label>
}

function TaskForm({ open, task, sources, templates, onClose, onSaved }: { open: boolean, task: Json | null, sources: Json[], templates: Json[], onClose: () => void, onSaved: () => void }) {
  const [form, setForm] = useState<Json>({}); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!open) return
    const c = task?.conditions || {}
    setForm({ name: task?.name || '', template_id: task?.template_id || templates[0]?.id || '', aliases: (c.aliases || []).join(', '), alias_mode: c.alias_mode || 'any', values: task?.values || {}, upgrade: false, source_ids: c.source_ids || sources.filter(s => s.enabled).map(s => s.id), expires_at: task?.expires_at?.slice(0, 10) || '' }); setError('')
  }, [open, task, sources, templates])
  function field(name: string, value: any) { setForm((old: Json) => ({ ...old, [name]: value })) }
  const template = templates.find(item => item.id === form.template_id)
  const fields = task && !form.upgrade ? task.template_fields || [] : template?.fields || []
  const selectedVersion = task && !form.upgrade ? task.template_version : template?.current_version
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    const defaults = Object.fromEntries(fields.filter((item: Json) => item.type === 'boolean').map((item: Json) => [item.key, false]))
    const conditions = { aliases: (form.aliases || '').split(',').map((v: string) => v.trim()).filter(Boolean), alias_mode: form.alias_mode, values: { ...defaults, ...(form.values || {}) }, source_ids: form.source_ids }
    try {
      const payload = { name: form.name, template_id: form.template_id, template_version: selectedVersion, expires_at: form.expires_at || null, conditions }
      if (task) await api(`/tasks/${task.id}`, { method: 'PATCH', body: JSON.stringify({ ...payload, version: task.version }) })
      else await api('/tasks', { method: 'POST', body: JSON.stringify(payload) })
      await onSaved(); onClose()
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }
  const enabled = sources.filter(source => source.enabled)
  return <Dialog open={open} title={task ? '编辑关注任务' : '新建关注任务'} onClose={onClose} wide><form className="form-grid" onSubmit={submit}>
    <label>任务名称<input value={form.name || ''} onChange={e => field('name', e.target.value)} required /></label>
    <label>业务模式<select value={form.template_id || ''} onChange={e => field('template_id', e.target.value)} disabled={!!task}>{task && !templates.some(item => item.id === task.template_id) && <option value={task.template_id}>{task.template_name}（已停用）</option>}{templates.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    {task && task.latest_template_version > task.template_version && <div className="alert warning full">该模式已有 v{task.latest_template_version}。<button type="button" onClick={() => setForm(old => ({ ...old, upgrade: !old.upgrade }))}>{form.upgrade ? '继续使用旧版' : '升级并编辑'}</button></div>}
    <label className="full">触发关键词（逗号分隔）<input value={form.aliases || ''} onChange={e => field('aliases', e.target.value)} required placeholder="ThinkBook, 战66" /></label>
    <label>关键词组合<select value={form.alias_mode || 'any'} onChange={e => field('alias_mode', e.target.value)}><option value="any">任一命中</option><option value="all">全部命中</option></select></label>
    <label>有效期<input type="date" value={form.expires_at || ''} onChange={e => field('expires_at', e.target.value)} /></label>
    {fields.map((definition: Json) => <FieldInput key={definition.key} field={definition} value={form.values?.[definition.key]} onChange={value => field('values', { ...(form.values || {}), [definition.key]: value })} />)}
    <fieldset className="full source-picker"><legend>来源（至少一个）</legend>{enabled.length ? enabled.map(source => <label key={source.id} className="check"><input type="checkbox" checked={(form.source_ids || []).includes(source.id)} onChange={e => field('source_ids', e.target.checked ? [...(form.source_ids || []), source.id] : (form.source_ids || []).filter((id: number) => id !== source.id))} />{source.display_name}<small>{source.source_type === 'contact' ? '联系人' : '群'} · {source.peer_id} · {source.account_name}</small></label>) : <p className="empty-inline">请先在“来源管理”中开启至少一个群或联系人。</p>}</fieldset>
    <div className="preview full">系统先按触发关键词筛选；自定义字段只做文字包含判断，未出现时进入待核对。</div>
    {error && <div className="alert error full">{error}</div>}<footer className="dialog-actions full"><button type="button" onClick={onClose}>取消</button><button className="primary" disabled={busy || !(form.source_ids || []).length || !selectedVersion}>{busy ? '保存中…' : '保存并启用'}</button></footer>
  </form></Dialog>
}

function Tasks({ sources, templates, notify, reloadShared }: { sources: Json[], templates: Json[], notify: (v: string) => void, reloadShared: () => Promise<void> }) {
  const [tasks, setTasks] = useState<Json[]>([]); const [selected, setSelected] = useState<Json | null>(null); const [formOpen, setFormOpen] = useState(false); const [mode, setMode] = useState('')
  const load = useCallback(async () => setTasks(await allPages('/tasks?include_archived=true')), [])
  useEffect(() => { load().catch(err => notify(err.message)) }, [load, notify])
  async function edit(task: Json) { setSelected(await api(`/tasks/${task.id}`)); setFormOpen(true) }
  async function status(task: Json, next: string) { try { await api(`/tasks/${task.id}`, { method: 'PATCH', body: JSON.stringify({ version: task.version, status: next }) }); await Promise.all([load(), reloadShared()]); notify('任务状态已更新') } catch (err) { notify((err as Error).message) } }
  async function saved() { await Promise.all([load(), reloadShared()]) }
  const active = tasks.filter(task => task.status === 'active'); const visible = mode ? tasks.filter(task => task.template_id === mode) : tasks
  const columns = mode ? templates.find(item => item.id === mode)?.fields?.filter((field: Json) => field.show_in_list) || [] : []
  const displayValue = (value: any) => value === true ? '是' : value === false ? '否' : value ?? '—'
  return <><div className="page-toolbar"><div><h2>任务概览</h2><p>只处理任务选中且全局允许采集的群或联系人。</p></div><button className="primary" onClick={() => { setSelected(null); setFormOpen(true) }} disabled={!templates.length}>＋ 新建任务</button></div>
    <div className="metrics"><div><span>运行中</span><strong>{active.length}</strong></div><div><span>新线索</span><strong>{tasks.reduce((n, t) => n + t.new_leads, 0)}</strong></div><div><span>待核对</span><strong>{tasks.reduce((n, t) => n + t.review_leads, 0)}</strong></div><div><span>已启用来源</span><strong>{sources.filter(s => s.enabled).length}</strong></div></div>
    <div className="filterbar"><select value={mode} onChange={e => setMode(e.target.value)}><option value="">全部业务模式</option>{templates.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
    {!tasks.length ? <div className="empty-state"><strong>还没有关注任务</strong><p>{sources.some(s => s.enabled) ? '创建第一个任务，开始整理线索。' : '先到来源管理选择允许保存的群或联系人。'}</p></div> : <section className="table-card"><table><thead><tr><th>任务</th><th>业务模式</th>{columns.map((field: Json) => <th key={field.key}>{field.label}</th>)}<th>状态</th><th>线索</th><th>最近处理</th><th>操作</th></tr></thead><tbody>{visible.map(task => <tr key={task.id}><td><strong>{task.name}</strong><small>任务版本 {task.current_revision}</small></td><td>{task.template_name}<small>模式 v{task.template_version}</small></td>{columns.map((field: Json) => <td key={field.key}>{displayValue(task.values?.[field.key])}</td>)}<td><Badge value={task.status} /></td><td>{task.new_leads} 新 / {task.review_leads} 待核对</td><td>{formatTime(task.last_processed_at)}</td><td><div className="row-actions"><button onClick={() => edit(task)}>编辑</button>{task.status === 'active' ? <button onClick={() => status(task, 'paused')}>暂停</button> : task.status !== 'archived' && <button onClick={() => status(task, 'active')}>启用</button>}{task.status !== 'completed' && task.status !== 'archived' && <button onClick={() => status(task, 'completed')}>完成</button>}{task.status === 'completed' && <button onClick={() => status(task, 'archived')}>归档</button>}</div></td></tr>)}</tbody></table></section>}
    <TaskForm open={formOpen} task={selected} sources={sources} templates={templates} onClose={() => setFormOpen(false)} onSaved={saved} />
  </>
}

function TemplateEditor({ open, template, onClose, onSaved }: { open: boolean, template: Json | null, onClose: () => void, onSaved: () => Promise<void> }) {
  const [name, setName] = useState(''); const [fields, setFields] = useState<Json[]>([]); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  useEffect(() => { if (open) { setName(template?.name || ''); setFields((template?.fields || []).map((field: Json) => ({ ...field, options_text: (field.options || []).map((o: Json) => o.label).join(', ') }))); setError('') } }, [open, template])
  function change(index: number, key: string, value: any) { setFields(old => old.map((field, i) => i === index ? { ...field, [key]: value } : field)) }
  function move(index: number, delta: number) { setFields(old => { const copy = [...old]; const target = index + delta; if (target < 0 || target >= copy.length) return old; [copy[index], copy[target]] = [copy[target], copy[index]]; return copy }) }
  function add() { setFields(old => [...old, { key: `field_${Date.now()}`, label: '新字段', type: 'text', required: false, show_in_list: false, match: false }]) }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    const payloadFields = fields.map(({ options_text, ...field }) => ({ ...field, options: field.type === 'select' ? field.role ? field.options : String(options_text || '').split(',').map(value => value.trim()).filter(Boolean).map(value => ({ value, label: value })) : undefined }))
    try {
      if (template) await api(`/templates/${template.id}`, { method: 'PATCH', body: JSON.stringify({ name, fields: payloadFields, version: template.version }) })
      else await api('/templates', { method: 'POST', body: JSON.stringify({ name, fields: payloadFields }) })
      await onSaved(); onClose()
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }
  return <Dialog open={open} title={template ? `编辑 ${template.name}` : '新建业务模式'} onClose={onClose} wide><form className="template-editor" onSubmit={save}>
    <label>模式名称<input value={name} onChange={e => setName(e.target.value)} required /></label>
    <div className="template-fields"><div className="field-row field-head"><span>字段标识 / 名称</span><span>类型 / 选项</span><span>规则</span><span>顺序</span></div>{fields.map((field, index) => <div className="field-row" key={field.key}>
      <div><input aria-label="字段标识" value={field.key} onChange={e => change(index, 'key', e.target.value)} disabled={!!field.role} /><input aria-label="字段名称" value={field.label} onChange={e => change(index, 'label', e.target.value)} /></div>
      <div><select value={field.type} onChange={e => change(index, 'type', e.target.value)} disabled={!!field.role}><option value="text">单行文本</option><option value="number">数字</option><option value="select">单选</option><option value="date">日期</option><option value="boolean">开关</option></select>{field.type === 'select' && <input aria-label="单选选项" placeholder="选项用逗号分隔" value={field.options_text || ''} onChange={e => change(index, 'options_text', e.target.value)} disabled={!!field.role} />}</div>
      <div><label className="inline-check"><input type="checkbox" checked={field.required} onChange={e => change(index, 'required', e.target.checked)} />必填</label><label className="inline-check"><input type="checkbox" checked={field.show_in_list} onChange={e => change(index, 'show_in_list', e.target.checked)} />列表列</label><label className="inline-check"><input type="checkbox" checked={field.match} onChange={e => change(index, 'match', e.target.checked)} />参与匹配</label></div>
      <div className="field-actions"><button type="button" onClick={() => move(index, -1)} aria-label="上移">↑</button><button type="button" onClick={() => move(index, 1)} aria-label="下移">↓</button>{!field.role && <button type="button" className="danger" onClick={() => setFields(old => old.filter((_, i) => i !== index))}>删除</button>}</div>
    </div>)}</div>
    <button type="button" onClick={add}>＋ 添加字段</button>{error && <div className="alert error">{error}</div>}<footer className="dialog-actions"><button type="button" onClick={onClose}>取消</button><button className="primary" disabled={busy}>{busy ? '保存中…' : '保存新版本'}</button></footer>
  </form></Dialog>
}

function Templates({ templates, reload, notify }: { templates: Json[], reload: () => Promise<void>, notify: (v: string) => void }) {
  const [selected, setSelected] = useState<Json | null>(null); const [open, setOpen] = useState(false)
  async function action(path: string, body: Json | undefined, message: string) { try { await api(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }); await reload(); notify(message) } catch (err) { notify((err as Error).message) } }
  return <><div className="page-toolbar"><div><h2>业务模式</h2><p>系统字段固定；这里配置业务字段、匹配项和任务列表列。</p></div><button className="primary" onClick={() => { setSelected(null); setOpen(true) }}>＋ 新建模式</button></div>
    <div className="template-grid">{templates.map(template => <article className="task-card" key={template.id}><header><div><h3>{template.name}</h3><small>v{template.current_version} · {template.fields.length} 个字段</small></div>{template.built_in ? <span className="badge">内置</span> : <Badge value={template.status} />}</header><p className="muted">列表显示 {template.fields.filter((field: Json) => field.show_in_list).length} 列，匹配字段 {template.fields.filter((field: Json) => field.match).length} 个。</p><footer><button onClick={() => { setSelected(template); setOpen(true) }}>编辑</button><button onClick={() => action(`/templates/${template.id}/copy`, undefined, '业务模式已复制')}>复制</button>{template.built_in ? <button onClick={() => confirm('恢复电脑经销商模式的出厂字段？已有任务不会改变。') && action(`/templates/${template.id}/reset`, { version: template.version }, '内置模式已重置')}>重置</button> : <button onClick={() => confirm('停用该业务模式？已有任务不会被删除。') && action(`/templates/${template.id}/archive`, { version: template.version }, '业务模式已停用')}>停用</button>}</footer></article>)}</div>
    <TemplateEditor open={open} template={selected} onClose={() => setOpen(false)} onSaved={reload} />
  </>
}

function Sources({ accounts, sources, reload, notify }: { accounts: Json[], sources: Json[], reload: () => Promise<void>, notify: (v: string) => void }) {
  const [q, setQ] = useState(''); const [busy, setBusy] = useState(false); const [tab, setTab] = useState<'group' | 'contact'>('group')
  async function refresh() { setBusy(true); try { const result = await api('/sources/refresh', { method: 'POST', body: JSON.stringify({ account_id: null }) }); await reload(); notify(result.errors.length ? '部分来源刷新失败，请检查连接。' : '群和联系人已刷新，新来源默认关闭。') } catch (err) { notify((err as Error).message) } finally { setBusy(false) } }
  async function toggle(source: Json) { try { await api(`/sources/${source.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !source.enabled, version: source.version }) }); await reload() } catch (err) { notify((err as Error).message) } }
  async function renameSource(source: Json) { const value = prompt('设置仅保存在本机的来源备注', source.user_alias || source.platform_name || ''); if (value === null) return; try { await api(`/sources/${source.id}`, { method: 'PATCH', body: JSON.stringify({ user_alias: value, version: source.version }) }); await reload() } catch (err) { notify((err as Error).message) } }
  async function renameAccount(account: Json) { const value = prompt('设置仅保存在本机的账号备注', account.user_alias || account.platform_name || '') ; if (value === null) return; try { await api(`/accounts/${account.account_id}`, { method: 'PATCH', body: JSON.stringify({ user_alias: value, version: account.version }) }); await reload() } catch (err) { notify((err as Error).message) } }
  async function addManual() { const accountId = accounts.length === 1 ? accounts[0].account_id : prompt('输入已连接的 QQ 号'); if (!accountId) return; const groupId = prompt('输入要验证的群号'); if (!groupId) return; try { await api('/sources/manual', { method: 'POST', body: JSON.stringify({ account_id: accountId, group_id: groupId }) }); await reload(); notify('账号群成员关系已验证；新来源保持关闭，请确认后开启。') } catch (err) { notify((err as Error).message) } }
  const filtered = sources.filter(source => source.source_type === tab && `${source.display_name} ${source.peer_id} ${source.account_name}`.toLowerCase().includes(q.toLowerCase()))
  return <><div className="page-toolbar"><div><h2>账号与来源</h2><p>只有明确开启的群和联系人消息才会写入业务数据库。</p></div><div className="toolbar-actions"><button onClick={addManual}>手动添加群</button><button onClick={refresh} disabled={busy}>{busy ? '刷新中…' : '↻ 刷新全部账号'}</button></div></div>
    <div className="account-grid">{accounts.length ? accounts.map(account => <article className="account-card" key={account.account_id}><div className="avatar">{(account.user_alias || account.platform_name || account.account_id).slice(0, 1)}</div><div><strong>{account.user_alias || account.platform_name || account.account_id}</strong><small>QQ {account.account_id}</small></div><Badge value={account.connected ? 'active' : 'paused'} /><button onClick={() => renameAccount(account)}>备注</button></article>) : <div className="empty-state">暂无 QQ 账号连接</div>}</div>
    <div className="tabs" role="tablist"><button role="tab" aria-selected={tab === 'group'} className={tab === 'group' ? 'active' : ''} onClick={() => setTab('group')}>群（{sources.filter(s => s.source_type === 'group').length}）</button><button role="tab" aria-selected={tab === 'contact'} className={tab === 'contact' ? 'active' : ''} onClick={() => setTab('contact')}>联系人（{sources.filter(s => s.source_type === 'contact').length}）</button></div>
    <div className="filterbar"><input type="search" value={q} onChange={e => setQ(e.target.value)} placeholder={`搜索${tab === 'group' ? '群名、群号' : '联系人、QQ号'}或账号`} /></div>
    <section className="table-card"><table><thead><tr><th>{tab === 'group' ? '群' : '联系人'}</th><th>账号</th><th>最后保存</th><th>状态</th><th>备注</th><th>采集</th></tr></thead><tbody>{filtered.map(source => <tr key={source.id}><td><strong>{source.display_name}</strong><small>{tab === 'group' ? '群' : 'QQ'} {source.peer_id}</small></td><td>{source.account_name}<small>QQ {source.account_id}</small></td><td>{formatTime(source.last_saved_at)}</td><td>{source.error_code ? <Badge value="failed" /> : <span className="muted">正常</span>}</td><td><button onClick={() => renameSource(source)}>设置备注</button></td><td><button className={`switch ${source.enabled ? 'on' : ''}`} aria-pressed={!!source.enabled} onClick={() => toggle(source)}><span /></button></td></tr>)}</tbody></table>{!filtered.length && <div className="empty-state">没有符合条件的{tab === 'group' ? '群' : '联系人'}</div>}</section>
  </>
}

function Leads({ tasks, notify }: { tasks: Json[], notify: (v: string) => void }) {
  const [items, setItems] = useState<Json[]>([]); const [nextCursor, setNextCursor] = useState<string | null>(null); const [filters, setFilters] = useState({ task: '', outcome: '', status: '', q: '', conflicts: false }); const [detail, setDetail] = useState<Json | null>(null); const [note, setNote] = useState('')
  const load = useCallback(async (cursor = '') => {
    const params = new URLSearchParams(); if (filters.task) params.set('task_id', filters.task); if (filters.outcome) params.set('outcome', filters.outcome); if (filters.status) params.set('manual_status', filters.status); if (filters.q) params.set('q', filters.q); if (filters.conflicts) params.set('include_conflicts', 'true')
    if (cursor) params.set('cursor', cursor); const result = await api(`/leads?${params}`)
    setItems(old => cursor ? [...old, ...result.items] : result.items); setNextCursor(result.next_cursor)
  }, [filters])
  useEffect(() => { const timer = setTimeout(() => load().catch(err => notify(err.message)), 150); return () => clearTimeout(timer) }, [load, notify])
  async function open(item: Json) { const value = await api(`/leads/${item.id}`); setDetail(value); setNote(value.note || '') }
  async function save(status = detail?.manual_status) { if (!detail) return; try { const value = await api(`/leads/${detail.id}`, { method: 'PATCH', body: JSON.stringify({ version: detail.version, manual_status: status, note, seen: true }) }); setDetail(value); await load(); notify('线索记录已保存') } catch (err) { notify((err as Error).message) } }
  return <><div className="page-toolbar"><div><h2>线索</h2><p>冲突结果默认收起；不明确方向始终进入待核对。</p></div><button onClick={async () => { await api('/leads/mark-read', { method: 'POST' }); load(); notify('新线索已标为已读') }}>全部标为已读</button></div>
    <div className="filterbar"><select value={filters.task} onChange={e => setFilters({ ...filters, task: e.target.value })}><option value="">全部任务</option>{tasks.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select><select value={filters.outcome} onChange={e => setFilters({ ...filters, outcome: e.target.value })}><option value="">全部判断</option><option value="match">匹配</option><option value="review">待核对</option><option value="conflict">冲突</option></select><select value={filters.status} onChange={e => setFilters({ ...filters, status: e.target.value })}><option value="">全部处理状态</option><option value="new">新线索</option><option value="contacted">已联系</option><option value="ignored">已忽略</option><option value="completed">已完成</option></select><input type="search" placeholder="搜索原文、发布者或任务" value={filters.q} onChange={e => setFilters({ ...filters, q: e.target.value })} /><label className="inline-check"><input type="checkbox" checked={filters.conflicts} onChange={e => setFilters({ ...filters, conflicts: e.target.checked })} />显示冲突</label></div>
    <section className="table-card"><table><thead><tr><th>任务 / 证据</th><th>发布者</th><th>方向</th><th>判断</th><th>处理状态</th><th>时间</th></tr></thead><tbody>{items.map(item => <tr key={item.id} onClick={() => open(item)} tabIndex={0} onKeyDown={e => e.key === 'Enter' && open(item)}><td><strong>{item.task_name}</strong><small className="ellipsis">{item.evidence.text}</small></td><td>{item.sender_name || item.sender_id || '原文已过期'}<small>{item.source_name}</small></td><td><Badge value={item.direction} /></td><td><Badge value={item.outcome} /></td><td><Badge value={item.manual_status} /></td><td>{formatTime(item.message_time || item.created_at)}</td></tr>)}</tbody></table>{!items.length && <div className="empty-state">没有符合筛选条件的线索</div>}{nextCursor && <div className="load-more"><button onClick={() => load(nextCursor)}>加载更多</button></div>}</section>
    <Dialog open={!!detail} title={detail?.task_name || '线索详情'} onClose={() => setDetail(null)} wide>{detail && <div className="lead-detail"><div className="detail-meta"><Badge value={detail.direction} /><Badge value={detail.outcome} /><span>任务版本 {detail.revision}</span><span>{detail.sender_name || detail.sender_id}</span><span>{formatTime(detail.message_time)}</span></div><section><h3>原文证据</h3><blockquote>{detail.evidence?.text || detail.raw_text || '证据已到期'}</blockquote></section><section><h3>条件比较</h3><div className="comparisons">{detail.comparisons.map((c: Json) => <div key={c.field}><span>{c.field}</span><strong>{c.actual || '未说明'}</strong><Badge value={c.state === 'match' ? 'match' : c.state === 'conflict' ? 'conflict' : 'review'} /></div>)}</div></section><label>人工备注<textarea rows={4} value={note} onChange={e => setNote(e.target.value)} placeholder="记录联系结果或人工更正" /></label><footer className="dialog-actions"><button onClick={() => save('ignored')}>标为已忽略</button><button onClick={() => save('contacted')}>标为已联系</button><button className="primary" onClick={() => save('completed')}>保存并完成</button></footer></div>}</Dialog>
  </>
}

function Messages({ sources, tasks, notify }: { sources: Json[], tasks: Json[], notify: (v: string) => void }) {
  const [items, setItems] = useState<Json[]>([]); const [nextCursor, setNextCursor] = useState<string | null>(null); const [q, setQ] = useState(''); const [source, setSource] = useState(''); const [result, setResult] = useState(''); const [detail, setDetail] = useState<Json | null>(null); const [replayOpen, setReplayOpen] = useState(false); const [replay, setReplay] = useState({ task: '', start: '', end: '' }); const [preview, setPreview] = useState<Json | null>(null)
  const load = useCallback(async (cursor = '') => { const p = new URLSearchParams(); if (q) p.set('q', q); if (source) p.set('source_id', source); if (result) p.set('result', result); if (cursor) p.set('cursor', cursor); const payload = await api(`/messages?${p}`); setItems(old => cursor ? [...old, ...payload.items] : payload.items); setNextCursor(payload.next_cursor) }, [q, source, result])
  useEffect(() => { const timer = setTimeout(() => load().catch(err => notify(err.message)), 180); return () => clearTimeout(timer) }, [load, notify])
  async function manual(taskId: string) { if (!detail || !taskId) return; try { await api(`/tasks/${taskId}/manual-leads`, { method: 'POST', body: JSON.stringify({ message_id: detail.id }) }); notify('原文已人工加入任务') } catch (err) { notify((err as Error).message) } }
  async function previewReplay() { try { setPreview(await api(`/tasks/${replay.task}/replay-preview`, { method: 'POST', body: JSON.stringify({ start_at: new Date(replay.start).toISOString(), end_at: new Date(replay.end).toISOString() }) })) } catch (err) { notify((err as Error).message) } }
  async function runReplay() { try { const value = await api(`/tasks/${replay.task}/replays`, { method: 'POST', body: JSON.stringify({ start_at: new Date(replay.start).toISOString(), end_at: new Date(replay.end).toISOString() }) }); notify(`已创建重跑作业，共 ${value.eligible_count} 条`); setReplayOpen(false); setPreview(null) } catch (err) { notify((err as Error).message) } }
  return <><div className="page-toolbar"><div><h2>保留期内原文</h2><p>原文按接收时间计算保留期，消息时间仍按平台时间显示。</p></div><button className="primary" onClick={() => setReplayOpen(true)}>重新匹配</button></div>
    <div className="filterbar"><input type="search" placeholder="输入关键词搜索原文" value={q} onChange={e => setQ(e.target.value)} /><select value={source} onChange={e => setSource(e.target.value)}><option value="">全部来源</option>{sources.map(s => <option key={s.id} value={s.id}>{s.display_name} · {s.account_name}</option>)}</select><select value={result} onChange={e => setResult(e.target.value)}><option value="">全部结果</option><option value="match">命中</option><option value="review">待核对</option><option value="conflict">冲突</option><option value="not_match">未命中</option><option value="pending">待处理</option><option value="failed">失败</option></select></div>
    <section className="table-card"><table><thead><tr><th>原文</th><th>来源 / 发布者</th><th>处理结果</th><th>消息时间</th></tr></thead><tbody>{items.map(item => <tr key={item.id} onClick={async () => setDetail(await api(`/messages/${item.id}`))}><td><span className="message-text">{item.text || '非文本消息'}</span></td><td>{item.source_name}<small>{item.sender_name || item.sender_id}</small></td><td><Badge value={item.result} /></td><td>{formatTime(item.message_time)}</td></tr>)}</tbody></table>{!items.length && <div className="empty-state">保留期内没有符合条件的原文</div>}{nextCursor && <div className="load-more"><button onClick={() => load(nextCursor)}>加载更多</button></div>}</section>
    <Dialog open={!!detail} title="原文详情" onClose={() => setDetail(null)} wide>{detail && <div className="lead-detail"><div className="detail-meta"><span>{detail.sender_name || detail.sender_id}</span><span>{detail.source_type === 'contact' ? '联系人' : '群'} {detail.peer_id}</span><span>{formatTime(detail.message_time)}</span></div><blockquote>{detail.text || '非文本消息'}</blockquote><label>人工加入任务<select defaultValue="" onChange={e => manual(e.target.value)}><option value="" disabled>选择任务…</option>{tasks.filter(t => t.status === 'active').map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label></div>}</Dialog>
    <Dialog open={replayOpen} title="重新匹配原文" onClose={() => { setReplayOpen(false); setPreview(null) }}><div className="form-stack"><label>任务<select value={replay.task} onChange={e => { setReplay({ ...replay, task: e.target.value }); setPreview(null) }}><option value="">选择任务…</option>{tasks.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label><label>开始时间<input type="datetime-local" value={replay.start} onChange={e => { setReplay({ ...replay, start: e.target.value }); setPreview(null) }} /></label><label>结束时间<input type="datetime-local" value={replay.end} onChange={e => { setReplay({ ...replay, end: e.target.value }); setPreview(null) }} /></label>{preview && <div className="preview"><strong>预计处理 {preview.eligible_count} 条</strong><br />{preview.limitation}</div>}<footer className="dialog-actions"><button onClick={previewReplay} disabled={!replay.task || !replay.start || !replay.end}>预览范围</button><button className="primary" onClick={runReplay} disabled={!preview}>确认重跑</button></footer></div></Dialog>
  </>
}

function ComponentStatus({ name, value }: { name: string, value: Json }) {
  return <article className={`component-status ${value?.state || 'warning'}`}><i /><div><strong>{name}</strong><span>{value?.message || '等待检测'}</span></div></article>
}

function Onboarding({ open, value, sources, onChange, onClose, reload, notify }: { open: boolean, value: Json, sources: Json[], onChange: (value: Json) => void, onClose: () => void, reload: () => Promise<void>, notify: (v: string) => void }) {
  const [busy, setBusy] = useState(false)
  async function check() { setBusy(true); try { onChange(await api('/onboarding/check', { method: 'POST' })) } catch (err) { notify((err as Error).message) } finally { setBusy(false) } }
  async function refreshSources() { setBusy(true); try { await api('/sources/refresh', { method: 'POST', body: JSON.stringify({ account_id: null }) }); await reload(); onChange(await api('/onboarding/check', { method: 'POST' })); notify('群和联系人已刷新') } catch (err) { notify((err as Error).message) } finally { setBusy(false) } }
  async function toggleSource(source: Json) { setBusy(true); try { await api(`/sources/${source.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !source.enabled, version: source.version }) }); await reload(); onChange(await api('/onboarding/check', { method: 'POST' })) } catch (err) { notify((err as Error).message) } finally { setBusy(false) } }
  async function close(action: 'dismiss' | 'complete') { setBusy(true); try { const result = await api('/onboarding', { method: 'PATCH', body: JSON.stringify({ action }) }); onChange(result); onClose() } catch (err) { notify((err as Error).message) } finally { setBusy(false) } }
  const components = value?.components || {}
  return <Dialog open={open} title="首次配置 Huginn Leads" onClose={onClose} wide><div className="onboarding">
    <ol><li><strong>检查本地服务</strong><span>Huginn Leads 与内置 NoneBot2 应显示正常。</span></li><li><strong>配置 NapCatQQ</strong><span>在 NapCat 中添加反向 WebSocket：<code>{value.websocket_url}</code></span><a className="external-link" href={NAPCAT_DOWNLOAD} target="_blank" rel="noreferrer">前往 NapCatQQ 官方 Releases 下载 ↗</a></li><li><strong>核对访问令牌</strong><span>本机 ONEBOT_ACCESS_TOKEN：{value.access_token_configured ? '已配置' : '未配置'}。网页不会读取或显示令牌。</span></li><li><strong>连接并登录 QQ</strong><span>启动 NapCatQQ，确认 WebSocket 与 QQ 登录状态。</span></li><li><strong>刷新并选择来源</strong><span>已启用 {value.enabled_sources || 0} 个群或联系人；新来源默认关闭。</span></li></ol>
    <div className="component-grid"><ComponentStatus name="Huginn Leads" value={components.huginn_leads} /><ComponentStatus name="NoneBot2" value={components.nonebot2} /><ComponentStatus name="NapCatQQ" value={components.napcatqq} /><ComponentStatus name="QQ 登录" value={components.qq} /></div>
    <div className="toolbar-actions"><button onClick={check} disabled={busy}>重新检测</button><button onClick={refreshSources} disabled={busy}>刷新群和联系人</button></div>
    <section className="onboarding-sources"><h3>选择允许采集的来源</h3>{sources.length ? <div>{sources.map(source => <label key={source.id}><span><strong>{source.display_name}</strong><small>{source.source_type === 'contact' ? '联系人' : '群'} · {source.peer_id} · {source.account_name}</small></span><button type="button" className={`switch ${source.enabled ? 'on' : ''}`} aria-label={`${source.enabled ? '停用' : '启用'} ${source.display_name}`} aria-pressed={!!source.enabled} onClick={() => toggleSource(source)} disabled={busy}><span /></button></label>)}</div> : <p className="muted">还没有来源，请先点击“刷新群和联系人”。</p>}</section>
    {!value.enabled_sources && <div className="alert warning">至少开启一个群或联系人后才能完成配置。</div>}
    <footer className="dialog-actions"><button onClick={() => close('dismiss')} disabled={busy}>暂时跳过</button><button className="primary" onClick={() => close('complete')} disabled={busy}>完成配置</button></footer>
  </div></Dialog>
}

function Operations({ status, reload, notify, openOnboarding }: { status: Json, reload: () => Promise<void>, notify: (v: string) => void, openOnboarding: () => void }) {
  const [backups, setBackups] = useState<Json[]>([]); const [failures, setFailures] = useState<Json[]>([]); const [retention, setRetention] = useState<Json>(status.retention || {}); const [preview, setPreview] = useState<Json | null>(null); const [retentionDirty, setRetentionDirty] = useState(false)
  const loadOperations = useCallback(async () => { const [b, f] = await Promise.all([api('/backups'), api('/jobs?status=failed')]); setBackups(b.items); setFailures(f.items) }, [])
  useEffect(() => { loadOperations().catch(() => {}) }, [loadOperations]); useEffect(() => { if (!retentionDirty) setRetention(status.retention || {}) }, [status.retention, retentionDirty])
  async function backup() { try { await api('/backups', { method: 'POST' }); await loadOperations(); notify('一致性备份已创建') } catch (err) { notify((err as Error).message) } }
  async function previewRetention() { try { const value = await api('/settings/retention-preview', { method: 'POST', body: JSON.stringify(retention) }); setPreview(value); if (!value.destructive) await saveRetention(value) } catch (err) { notify((err as Error).message) } }
  async function saveRetention(value = preview) { if (!value) return; try { await api('/settings/retention', { method: 'PATCH', body: JSON.stringify({ ...retention, confirmation_token: value.confirmation_token }) }); setPreview(null); setRetentionDirty(false); await reload(); notify('保留设置已更新') } catch (err) { notify((err as Error).message) } }
  async function retry(job: Json) { try { await api(`/jobs/${job.id}/retry`, { method: 'POST' }); await loadOperations(); await reload(); notify('失败作业已重新排队') } catch (err) { notify((err as Error).message) } }
  const components = status.components || {}
  return <><div className="page-toolbar"><div><h2>系统状态</h2><p>连接恢复只代表当前在线，不代表离线消息已补回。</p></div><div className="toolbar-actions"><button onClick={openOnboarding}>配置向导</button><button onClick={backup}>创建备份</button></div></div>
    <div className="component-grid"><ComponentStatus name="Huginn Leads" value={components.huginn_leads} /><ComponentStatus name="NoneBot2" value={components.nonebot2} /><ComponentStatus name="NapCatQQ" value={components.napcatqq} /><ComponentStatus name="QQ 登录" value={components.qq} /></div>
    <section className="napcat-card"><div><h3>由 NapCatQQ 提供 QQ 接入</h3><p>NapCatQQ 是基于 NTQQ 的现代化协议端框架，通过 OneBot 连接 Huginn Leads。它是独立的第三方项目。</p></div><div className="napcat-actions"><a href={NAPCAT_HOME} target="_blank" rel="noreferrer">了解 NapCatQQ ↗</a><a className="primary-link" href={NAPCAT_DOWNLOAD} target="_blank" rel="noreferrer">官方下载</a></div></section>
    <div className="metrics"><div><span>在线账号</span><strong>{status.accounts_connected || 0}</strong></div><div><span>待处理</span><strong>{status.backlog || 0}</strong></div><div><span>失败</span><strong>{status.failed || 0}</strong></div><div><span>磁盘可用</span><strong>{bytes(status.disk_free_bytes)}</strong></div></div>
    <div className="operations-grid"><section className="panel"><h3>保存与数据</h3><dl><div><dt>最后接收</dt><dd>{formatTime(status.last_received_at)}</dd></div><div><dt>最后保存成功</dt><dd>{formatTime(status.last_saved_at)}</dd></div><div><dt>原文缓存</dt><dd>{status.raw_messages || 0} 条</dd></div><div><dt>有效线索</dt><dd>{status.leads || 0} 条</dd></div><div><dt>数据库</dt><dd>{status.database_ok ? '完整性正常' : '需要维护'}</dd></div><div><dt>数据目录</dt><dd className="path">{status.data_path}</dd></div></dl></section>
      <section className="panel"><h3>保留期限</h3><div className="retention-grid"><label>原文（天）<input type="number" min="1" value={retention.raw_days || 7} onChange={e => { setRetention({ ...retention, raw_days: Number(e.target.value) }); setPreview(null); setRetentionDirty(true) }} /></label><label>线索证据（天）<input type="number" min="1" value={retention.lead_days || 30} onChange={e => { setRetention({ ...retention, lead_days: Number(e.target.value) }); setPreview(null); setRetentionDirty(true) }} /></label><label>诊断（天）<input type="number" min="1" value={retention.diagnostic_days || 7} onChange={e => { setRetention({ ...retention, diagnostic_days: Number(e.target.value) }); setPreview(null); setRetentionDirty(true) }} /></label><label>备份份数<input type="number" min="1" value={retention.backup_keep || 7} onChange={e => { setRetention({ ...retention, backup_keep: Number(e.target.value) }); setPreview(null); setRetentionDirty(true) }} /></label></div>{preview?.destructive && <div className="alert warning">预计删除：原文 {preview.estimated_deletions.messages}、线索 {preview.estimated_deletions.leads}、诊断 {preview.estimated_deletions.diagnostics}、备份 {preview.estimated_deletions.backups}。延长期限不能恢复已删数据。</div>}<div className="panel-actions">{preview?.destructive ? <button className="danger" onClick={() => saveRetention()}>确认缩短并清理</button> : <button onClick={previewRetention}>保存设置</button>}</div></section>
      <section className="panel"><h3>最近备份</h3>{backups.length ? <ul className="backup-list">{backups.slice(0, 7).map(item => <li key={item.name}><strong>{item.name}</strong><span>{bytes(item.size)} · {formatTime(item.modified_at)}</span></li>)}</ul> : <p className="muted">还没有备份。</p>}<p className="help">恢复前先停止工作台，再运行 restore.cmd。网页不会在线覆盖数据库。</p></section>
      <section className="panel"><h3>疑似中断区间</h3>{status.gaps?.length ? <ul className="backup-list">{status.gaps.map((gap: Json) => <li key={gap.id}><strong>QQ {gap.account_id}</strong><span>{formatTime(gap.started_at)} — {gap.ended_at ? formatTime(gap.ended_at) : '尚未恢复'}</span></li>)}</ul> : <p className="muted">暂无记录到的连接缺口。</p>}</section>
      <section className="panel full-panel"><h3>失败作业</h3>{failures.length ? <ul className="failure-list">{failures.map(job => <li key={job.id}><div><strong>{job.task_name}</strong><span>{job.error_code || 'unknown_error'} · 已尝试 {job.attempts} 次 · {formatTime(job.updated_at)}</span></div><button onClick={() => retry(job)}>重试</button></li>)}</ul> : <p className="muted">当前没有失败作业。</p>}</section></div>
  </>
}

export default function App() {
  const [auth, setAuth] = useState<Json | null>(null); const [view, setView] = useState<View>('tasks'); const [menu, setMenu] = useState(false); const [accounts, setAccounts] = useState<Json[]>([]); const [sources, setSources] = useState<Json[]>([]); const [tasks, setTasks] = useState<Json[]>([]); const [templates, setTemplates] = useState<Json[]>([]); const [status, setStatus] = useState<Json>({}); const [onboarding, setOnboarding] = useState<Json>({}); const [wizard, setWizard] = useState(false); const [toast, setToast] = useState('')
  const notify = useCallback((message: string) => { setToast(message); setTimeout(() => setToast(''), 3500) }, [])
  const loadShared = useCallback(async () => {
    const [a, s, t, m, h, o] = await Promise.all([api('/accounts'), api('/sources'), allPages('/tasks?include_archived=true'), api('/templates'), api('/status'), api('/onboarding')])
    setAccounts(a.items); setSources(s.items); setTasks(t); setTemplates(m.items); setStatus(h); setOnboarding(o); if (!o.seen) setWizard(true)
  }, [])
  useEffect(() => { api('/auth/status').then(value => { csrf = value.csrf_token || ''; setAuth(value) }).catch(err => setAuth({ initialized: true, authenticated: false, error: err.message })) }, [])
  useEffect(() => { if (!auth?.authenticated) return; loadShared().catch(err => notify(err.message)); const timer = setInterval(() => api('/status').then(setStatus).catch(() => {}), 10000); return () => clearInterval(timer) }, [auth?.authenticated, loadShared, notify])
  const content = useMemo(() => {
    if (view === 'tasks') return <Tasks sources={sources} templates={templates} notify={notify} reloadShared={loadShared} />
    if (view === 'templates') return <Templates templates={templates} reload={loadShared} notify={notify} />
    if (view === 'sources') return <Sources accounts={accounts} sources={sources} reload={loadShared} notify={notify} />
    if (view === 'leads') return <Leads tasks={tasks} notify={notify} />
    if (view === 'messages') return <Messages sources={sources} tasks={tasks} notify={notify} />
    return <Operations status={status} reload={loadShared} notify={notify} openOnboarding={() => setWizard(true)} />
  }, [view, sources, accounts, tasks, templates, status, notify, loadShared])
  if (!auth) return <div className="loading">正在打开工作台…</div>
  if (!auth.authenticated) return <Auth initialized={auth.initialized} onDone={token => { csrf = token; setAuth({ initialized: true, authenticated: true }) }} />
  async function logout() { await api('/auth/logout', { method: 'POST' }); csrf = ''; setAuth({ initialized: true, authenticated: false }) }
  return <div className="app-shell"><aside className={menu ? 'sidebar open' : 'sidebar'}><div className="brand"><img src="/huginn-leads-logo.png" alt="" /><span><strong>Huginn Leads</strong><small>消息工作台</small></span></div><nav>{(Object.keys(labels) as View[]).map(name => <button key={name} className={view === name ? 'active' : ''} onClick={() => { setView(name); setMenu(false) }}><span>{({ tasks: '▦', templates: '◇', leads: '◎', sources: '⌁', messages: '☷', operations: '◌' } as Record<View, string>)[name]}</span>{labels[name][0]}</button>)}</nav><div className="sidebar-footer"><div className="live"><i className={status.accounts_connected ? 'online' : ''} /><span>{status.accounts_connected || 0} 个账号在线</span></div><button onClick={logout}>退出登录</button></div></aside><main className="workspace"><header className="topbar"><button className="menu-button" onClick={() => setMenu(!menu)} aria-label="打开导航">☰</button><div><h1>{labels[view][0]}</h1><span>{labels[view][1]}</span></div><button className="status-link" onClick={() => onboarding.completed ? setView('operations') : setWizard(true)}><i className={!onboarding.completed || status.failed ? 'problem' : 'online'} />{!onboarding.completed ? '首次配置未完成' : status.failed ? `${status.failed} 个失败任务` : `最近保存 ${formatTime(status.last_saved_at)}`}</button></header><div className="content">{content}</div></main>{menu && <button className="scrim" aria-label="关闭导航" onClick={() => setMenu(false)} />}<Onboarding open={wizard} value={onboarding} sources={sources} onChange={setOnboarding} onClose={() => setWizard(false)} reload={loadShared} notify={notify} />{toast && <div className="toast" role="status">{toast}</div>}</div>
}
