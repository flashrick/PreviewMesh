package main

import (
	"strings"
	"testing"
)

// TestEvidenceTable checks detailed feedback without inventing successful checks.
func TestEvidenceTable(t *testing.T) {
	e := reportEvidence{Build: "success", Runtime: map[string]string{"deployment": "ready 1/1", "pods": "ready 0/1; CrashLoopBackOff", "service": "present", "ingress": "present"}, HTTPStatus: 503, HTTPVerification: "failure", Rollback: "failed", Cleanup: "not_attempted"}
	body := e.markdown()
	for _, want := range []string{"| Build | success |", "ready 1/1", "CrashLoopBackOff", "HTTP 503", "| HTTP body and commit verification | failure |", "| Rollback | failed |", "| Cleanup | not&#95;attempted |"} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %q in %s", want, body)
		}
	}
	missing := (reportEvidence{}).markdown()
	if strings.Contains(missing, "success") || !strings.Contains(missing, "not checked / no response") {
		t.Fatal(missing)
	}
	e.Runtime["ingress"] = "host|<script>\n[link](evil)"
	if strings.Contains(e.markdown(), "<script>") || strings.Contains(e.markdown(), "host|") {
		t.Fatal("unescaped API text")
	}
}
