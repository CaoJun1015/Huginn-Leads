CREATE INDEX IF NOT EXISTS idx_evaluations_task_processed
  ON evaluations(task_id, processed_at DESC);
