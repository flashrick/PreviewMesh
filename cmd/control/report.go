package main

import (
	"fmt"
	"os"
	"previewmesh/internal/statusrecord"
	"regexp"
	"strings"
)

// errorDisplayLimit bounds tool stderr in PR comments; it exceeds the normal cell limit
// because the actionable part of a failure is often at the end of the output.
const errorDisplayLimit = 1000

var (
	// Runners redact their own tokens, but a comment is the last boundary before readers,
	// so also catch URL credentials and GitHub tokens the tools may have echoed.
	urlCredentials = regexp.MustCompile(`://[^/\s@]+@`)
	githubTokens   = regexp.MustCompile(`\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})`)
	// escapeCell neutralizes Markdown, HTML, mentions, and issue links in API or tool text.
	escapeCell = strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", "|", "&#124;", "`", "&#96;", "*", "&#42;", "_", "&#95;", "[", "&#91;", "]", "&#93;", "\\", "&#92;", "@", "&#64;", "#", "&#35;", "\n", " ", "\r", " ")
)

// publicError redacts credentials, then keeps both ends of an oversized failure message.
func publicError(value string) string {
	if secret := os.Getenv("SOURCE_TOKEN"); len(secret) >= 4 {
		value = strings.ReplaceAll(value, secret, "<REDACTED>")
	}
	value = urlCredentials.ReplaceAllString(value, "://<REDACTED>@")
	value = githubTokens.ReplaceAllString(value, "<REDACTED>")
	if len(value) > errorDisplayLimit {
		const marker = " ... "
		head := errorDisplayLimit / 4
		tail := errorDisplayLimit - head - len(marker)
		value = value[:head] + marker + value[len(value)-tail:]
	}
	// Byte slicing can split a multibyte character at either cut.
	return strings.ToValidUTF8(value, "")
}

// reportEvidence contains only the observations intended for PR feedback.
type reportEvidence struct {
	Build                string                `json:"-"`
	Runtime              map[string]string     `json:"runtime"`
	HTTPStatus           int                   `json:"http_status"`
	HTTPVerification     string                `json:"http_verification"`
	RequestedSHA         string                `json:"requested_sha"`
	ServedSHA            string                `json:"served_sha"`
	RevisionVerification string                `json:"revision_verification"`
	FailedStage          string                `json:"failed_stage"`
	Rollback             string                `json:"rollback"`
	Cleanup              string                `json:"cleanup"`
	Result               string                `json:"result"`
	Error                string                `json:"error"`
	StageTimings         []statusrecord.Timing `json:"stage_timings"`
}

// markdown keeps verbose evidence available without overwhelming the result summary.
func (e reportEvidence) markdown() string {
	cell := func(value string) string {
		if value == "" {
			return "not checked / unavailable"
		}
		if len(value) > 500 {
			value = value[:500]
		}
		return escapeCell.Replace(value)
	}
	http := "not checked / no response"
	if e.HTTPStatus != 0 {
		http = fmt.Sprintf("last response: HTTP %d", e.HTTPStatus)
	}
	rows := [][2]string{
		{"Build", e.Build}, {"Deployment", e.Runtime["deployment"]},
		{"Pods", e.Runtime["pods"]}, {"Service", e.Runtime["service"]}, {"Ingress", e.Runtime["ingress"]},
		{"HTTP /health", http}, {"HTTP body and commit verification", e.HTTPVerification},
		{"Requested commit", e.RequestedSHA}, {"Served commit", e.ServedSHA},
		{"Revision verification", e.RevisionVerification}, {"Failed stage", e.FailedStage},
		{"Rollback", e.Rollback}, {"Cleanup", e.Cleanup},
	}
	body := "\n<details>\n<summary><strong>Deployment checks</strong></summary>\n\n| Check | Observed result |\n| --- | --- |\n"
	for _, row := range rows {
		body += "| " + row[0] + " | " + cell(row[1]) + " |\n"
		// Show the cause beside its stage; successful attempts have no error row.
		if row[0] == "Failed stage" && e.Error != "" {
			body += "| Error | " + escapeCell.Replace(publicError(e.Error)) + " |\n"
		}
	}
	return body + "\nRuntime observations are a snapshot at the end of the attempt, after rollback if attempted. Service/Ingress presence alone does not establish reachability.\n\n</details>\n"
}
