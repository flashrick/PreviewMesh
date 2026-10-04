// Package statusrecord defines the credential-free evidence published on source PRs.
package statusrecord

const Marker = "<!-- previewmesh-status-v1:"

// Timing preserves the interval recorded by the deployment runner.
type Timing struct {
	Stage           string  `json:"stage"`
	StartedAtUTC    string  `json:"started_at_utc"`
	EndedAtUTC      string  `json:"ended_at_utc"`
	DurationSeconds float64 `json:"duration_seconds"`
	Result          string  `json:"result"`
}

// Record links an observation to one exact GitHub commit status.
type Record struct {
	StatusID             int64             `json:"status_id"`
	Repository           string            `json:"repository"`
	PR                   string            `json:"pr"`
	SHA                  string            `json:"sha"`
	State                string            `json:"state"`
	RunURL               string            `json:"run_url"`
	URL                  string            `json:"url"`
	Build                string            `json:"build"`
	Result               string            `json:"result"`
	Runtime              map[string]string `json:"runtime"`
	HTTPStatus           int               `json:"http_status"`
	HTTPVerification     string            `json:"http_verification"`
	RequestedSHA         string            `json:"requested_sha"`
	ServedSHA            string            `json:"served_sha"`
	RevisionVerification string            `json:"revision_verification"`
	FailedStage          string            `json:"failed_stage"`
	Rollback             string            `json:"rollback"`
	Cleanup              string            `json:"cleanup"`
	StageTimings         []Timing          `json:"stage_timings"`
}
