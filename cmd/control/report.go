package main

import (
	"fmt"
	"strings"
)

// reportEvidence contains only the observations intended for PR feedback.
type reportEvidence struct {
	Build            string            `json:"-"`
	Runtime          map[string]string `json:"runtime"`
	HTTPStatus       int               `json:"http_status"`
	HTTPVerification string            `json:"http_verification"`
	ServedSHA        string            `json:"served_sha"`
	FailedStage      string            `json:"failed_stage"`
	Rollback         string            `json:"rollback"`
	Cleanup          string            `json:"cleanup"`
}

// markdown uses explicit unknowns so missing evidence never looks successful.
func (e reportEvidence) markdown() string {
	cell := func(value string) string {
		if value == "" {
			return "not checked / unavailable"
		}
		if len(value) > 500 {
			value = value[:500]
		}
		return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", "|", "&#124;", "`", "&#96;", "*", "&#42;", "_", "&#95;", "[", "&#91;", "]", "&#93;", "\\", "&#92;", "\n", " ", "\r", " ").Replace(value)
	}
	http := "not checked / no response"
	if e.HTTPStatus != 0 {
		http = fmt.Sprintf("last response: HTTP %d", e.HTTPStatus)
	}
	rows := [][2]string{
		{"Build", e.Build}, {"Deployment", e.Runtime["deployment"]},
		{"Pods", e.Runtime["pods"]}, {"Service", e.Runtime["service"]}, {"Ingress", e.Runtime["ingress"]},
		{"HTTP /health", http}, {"HTTP body and commit verification", e.HTTPVerification},
		{"Served commit", e.ServedSHA}, {"Failed stage", e.FailedStage},
		{"Rollback", e.Rollback}, {"Cleanup", e.Cleanup},
	}
	body := "\n| Check | Observed result |\n| --- | --- |\n"
	for _, row := range rows {
		body += "| " + row[0] + " | " + cell(row[1]) + " |\n"
	}
	return body + "\nRuntime observations are a snapshot at the end of the attempt, after rollback if attempted. Service/Ingress presence alone does not establish reachability.\n"
}
