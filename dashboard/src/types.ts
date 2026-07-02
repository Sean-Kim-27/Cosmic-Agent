export type StreamEvent =
  | { event: "metadata"; data: { provider: string; model: string } }
  | { event: "token"; data: { text: string } }
  | { event: "done"; data: { provider: string; model: string; parse_cgi: boolean } }
  | { event: "error"; data: { code: string; message: string } };

export interface ChatStreamRequest {
  message: string;
  provider?: string;
  model?: string;
  session_id?: string;
  parse_cgi: boolean;
  history: Array<{ role: "user" | "assistant"; content: string }>;
}

export interface ChatHistoryMessage {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  provider: string | null;
  model: string | null;
  created_at: string;
}

export interface ChatHistoryResponse {
  session_id: string;
  messages: ChatHistoryMessage[];
  cgi_context: {
    interactions: Array<{
      id: string;
      session_id: string | null;
      user_message: string;
      assistant_answer: string;
      created_at: string;
    }>;
  };
}

export interface ChatSessionSummary {
  session_id: string;
  message_count: number;
  preview: string;
  provider: string | null;
  model: string | null;
  updated_at: string;
}

export interface ChatSessionListResponse {
  sessions: ChatSessionSummary[];
}

export interface ChatSessionClearResponse {
  session_id: string;
  deleted_messages: number;
}

export interface DashboardJobStatusCount {
  status: JobStatus;
  count: number;
}

export interface DashboardHourEntry {
  hour_utc: number;
  count: number;
}

export interface DashboardSessionActivity {
  session_id: string;
  message_count: number;
}

export interface DashboardSummary {
  job_status_counts: DashboardJobStatusCount[];
  total_jobs: number;
  success_rate_percent: number;
  hourly_jobs_last_24h: DashboardHourEntry[];
  top_sessions_by_message_count: DashboardSessionActivity[];
  generated_at: string;
}

export type ChatMessage = {
  role: "user" | "assistant" | "system";
  content: string;
};

export type JobStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED" | "QUOTA_LOCKED";

export interface CGIParseJob {
  id: string;
  session_id: string | null;
  user_message: string;
  assistant_answer: string;
  status: JobStatus;
  attempts: number;
  max_attempts: number;
  last_error_type: string | null;
  last_error_message: string | null;
  next_run_at: string | null;
  locked_at: string | null;
  completed_at: string | null;
  interaction_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobRetryResponse {
  reset_count: number;
  statuses: string[];
  processing_scheduled: boolean;
}
