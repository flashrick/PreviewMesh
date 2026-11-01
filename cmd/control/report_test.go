package main

import (
	"strings"
	"testing"
	"unicode/utf8"
)

// TestEvidenceTable checks detailed feedback without inventing successful checks.
func TestEvidenceTable(t *testing.T) {
	e := reportEvidence{Build: "success", Runtime: map[string]string{"deployment": "ready 1/1", "pods": "ready 0/1; CrashLoopBackOff", "service": "present", "ingress": "present"}, HTTPStatus: 503, HTTPVerification: "failure", RequestedSHA: strings.Repeat("a", 40), ServedSHA: strings.Repeat("b", 40), RevisionVerification: "failure", Rollback: "failed", Cleanup: "not_attempted"}
	body := e.markdown()
	for _, want := range []string{"| Build | success |", "ready 1/1", "CrashLoopBackOff", "HTTP 503", "| HTTP body and commit verification | failure |", "| Requested commit | " + strings.Repeat("a", 40) + " |", "| Served commit | " + strings.Repeat("b", 40) + " |", "| Revision verification | failure |", "| Rollback | failed |", "| Cleanup | not&#95;attempted |"} {
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

// TestEvidenceTableShowsError puts the failure cause beside its stage and only when one exists.
func TestEvidenceTableShowsError(t *testing.T) {
	e := reportEvidence{FailedStage: "image_pull", Error: `image pull check failed: pods "demo" not found`}
	want := "| Failed stage | image&#95;pull |\n| Error | image pull check failed: pods \"demo\" not found |\n"
	if body := e.markdown(); !strings.Contains(body, want) {
		t.Fatalf("missing %q in %s", want, body)
	}
	if body := (reportEvidence{FailedStage: "image_pull"}).markdown(); strings.Contains(body, "| Error |") {
		t.Fatalf("error row without an error: %s", body)
	}
}

// TestEvidenceTableNeutralizesError keeps tool output from changing the comment or notifying anyone.
func TestEvidenceTableNeutralizesError(t *testing.T) {
	body := reportEvidence{Error: "a|b\n| Forged | row |\n<script>x</script> [link](evil) @someone #99"}.markdown()
	for _, unsafe := range []string{"a|b", "\n| Forged", "<script>", "[link]", "@someone", "#99"} {
		if strings.Contains(body, unsafe) {
			t.Errorf("unescaped %q in %s", unsafe, body)
		}
	}
}

// TestPublicErrorRedactsCredentials covers secrets that reach the comment boundary.
func TestPublicErrorRedactsCredentials(t *testing.T) {
	t.Setenv("SOURCE_TOKEN", "source-secret")
	fineGrained := "github_pat_" + strings.Repeat("c", 30)
	got := publicError("clone https://user:url-secret@github.com/a/b failed with ghs_" + strings.Repeat("b", 30) + " " + fineGrained + " source-secret")
	for _, secret := range []string{"url-secret", "ghs_", fineGrained, "source-secret"} {
		if strings.Contains(got, secret) {
			t.Errorf("%q leaked in %q", secret, got)
		}
	}
	if !strings.Contains(got, "https://<REDACTED>@github.com/a/b") {
		t.Errorf("URL context lost in %q", got)
	}
}

// TestPublicErrorKeepsBothEnds preserves the failing command and its final diagnostic.
func TestPublicErrorKeepsBothEnds(t *testing.T) {
	got := publicError("first-line " + strings.Repeat("é", 2000) + " final-cause")
	if len(got) > errorDisplayLimit || !utf8.ValidString(got) {
		t.Fatalf("length %d, valid UTF-8 %t", len(got), utf8.ValidString(got))
	}
	if !strings.HasPrefix(got, "first-line") || !strings.HasSuffix(got, "final-cause") {
		t.Fatalf("truncated output lost an end: %q", got)
	}
}
