// Tests cover pull request policy and exact registration matching.
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestReportStatus verifies visible PR results and independent reporting failures.
func TestReportStatus(t *testing.T) {
	sha := strings.Repeat("a", 40)
	runURL := "https://github.com/owner/control/actions/runs/1"
	previewURL := "http://pm-r12-pr3.preview.test:18080"
	for _, tc := range []struct {
		name, status, description, target string
		comment, preview                  bool
		statusCode, commentCode           int
	}{
		{"pending", "pending", "Building preview", runURL, false, false, 201, 201},
		{"ready", "success", "Preview ready at verified revision", previewURL, true, true, 201, 201},
		{"removed", "success", "Preview removed", runURL, true, false, 201, 201},
		{"failed", "failure", "Preview attempt failed", runURL, true, false, 201, 201},
		{"superseded", "failure", "Preview attempt superseded by newer revision", runURL, true, false, 201, 201},
		{"status denied", "success", "Preview ready", previewURL, true, true, 403, 201},
		{"comment denied", "success", "Preview ready", previewURL, true, true, 201, 403},
	} {
		t.Run(tc.name, func(t *testing.T) {
			statusCalls, commentCalls := 0, 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				if req.Method != "POST" || req.Header.Get("Authorization") != "Bearer test" {
					t.Errorf("unexpected request method or authentication")
				}
				var payload map[string]string
				if err := json.NewDecoder(req.Body).Decode(&payload); err != nil {
					t.Fatal(err)
				}
				switch req.URL.Path {
				case "/repos/owner/demo/statuses/" + sha:
					statusCalls++
					if payload["state"] != tc.status || payload["target_url"] != tc.target || payload["context"] != "PreviewMesh" {
						t.Errorf("unexpected status: %v", payload)
					}
					w.WriteHeader(tc.statusCode)
				case "/repos/owner/demo/issues/3/comments":
					commentCalls++
					body := payload["body"]
					for _, want := range []string{sha, tc.description, runURL, "**" + tc.status + "**"} {
						if !strings.Contains(body, want) {
							t.Errorf("missing %q in comment %q", want, body)
						}
					}
					if strings.Contains(body, "[Open preview]") != tc.preview || strings.Contains(body, previewURL) != tc.preview {
						t.Errorf("incorrect preview link: %s", body)
					}
					w.WriteHeader(tc.commentCode)
				default:
					t.Errorf("unexpected path: %s", req.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer srv.Close()
			err := reportStatus(api{base: srv.URL, token: "test", client: srv.Client()}, registration{Source: "owner/demo"}, "3", sha, tc.status, tc.description, tc.target, runURL, tc.comment)
			wantError := tc.statusCode >= 400 || tc.commentCode >= 400
			if (err != nil) != wantError || statusCalls != 1 || (commentCalls == 1) != tc.comment {
				t.Fatalf("error=%v status calls=%d comment calls=%d", err, statusCalls, commentCalls)
			}
		})
	}
}

// TestReportRejectsInvalidInput ensures malformed input causes no remote writes.
func TestReportRejectsInvalidInput(t *testing.T) {
	for _, tc := range []struct{ sha, status, target, run string }{
		{"bad", "success", "https://example.com", "https://example.com/run"},
		{strings.Repeat("a", 40), "unknown", "https://example.com", "https://example.com/run"},
		{strings.Repeat("a", 40), "pending", "https://example.com", "https://example.com/run"},
		{strings.Repeat("a", 40), "success", "javascript:alert(1)", "https://example.com/run"},
		{strings.Repeat("a", 40), "success", "https://example.com/>)", "https://example.com/run"},
		{strings.Repeat("a", 40), "success", "https://user:secret@example.com", "https://example.com/run"},
		{strings.Repeat("a", 40), "success", "https://example.com", ""},
	} {
		// A nil client would panic if validation accidentally issued a request.
		if err := reportStatus(api{}, registration{}, "3", tc.sha, tc.status, "Preview", tc.target, tc.run, true); err == nil {
			t.Errorf("accepted invalid input: %+v", tc)
		}
	}
}

// TestCurrentPRPolicy checks open, closed, forked, and permission-denied PRs.
func TestCurrentPRPolicy(t *testing.T) {
	for _, tc := range []struct {
		name, state string
		merged      bool
		head        int
		push        bool
		ok          bool
	}{
		{"write author", "open", false, 12, true, true}, {"maintainer effective push", "open", false, 12, true, true},
		{"read author", "open", false, 12, false, false}, {"fork", "open", false, 13, true, false},
		{"closed former author", "closed", false, 12, false, true},
		{"merged former author", "open", true, 12, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			permissionCalls := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				// Return only the API responses needed for this policy case.
				switch r.URL.Path {
				case "/repositories/12":
					fmt.Fprint(w, `{"id":12,"full_name":"owner/demo"}`)
				case "/repos/owner/demo/pulls/3":
					fmt.Fprintf(w, `{"number":3,"state":%q,"merged":%t,"head":{"sha":%q,"repo":{"id":%d}},"base":{"repo":{"id":12}},"user":{"login":"author"}}`, tc.state, tc.merged, strings.Repeat("a", 40), tc.head)
				case "/repos/owner/demo/collaborators/author/permission":
					permissionCalls++
					fmt.Fprintf(w, `{"permission":"maintain","user":{"permissions":{"push":%t}}}`, tc.push)
				default:
					t.Errorf("unexpected request %s", r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer srv.Close()
			s, err := inspect(api{base: srv.URL, token: "test", client: srv.Client()}, registration{ID: "12", Source: "owner/demo"}, "3")
			if (err == nil) != tc.ok {
				t.Fatalf("state=%+v err=%v", s, err)
			}
			if tc.state == "closed" || tc.merged {
				if s.State != "closed" || s.Merged != tc.merged || s.Eligible || permissionCalls != 0 {
					t.Fatalf("cleanup policy state=%+v permission calls=%d", s, permissionCalls)
				}
			}
		})
	}
}

// TestRepeatedNotificationInspection requires fresh state and authorization for every delivery.
func TestRepeatedNotificationInspection(t *testing.T) {
	sha := strings.Repeat("a", 40)
	reads := map[string]int{}
	allow := true
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reads[r.URL.Path]++
		switch r.URL.Path {
		case "/repositories/12":
			fmt.Fprint(w, `{"id":12,"full_name":"owner/demo"}`)
		case "/repos/owner/demo/pulls/3":
			fmt.Fprintf(w, `{"number":3,"state":"open","head":{"sha":%q,"repo":{"id":12}},"base":{"repo":{"id":12}},"user":{"login":"author"}}`, sha)
		case "/repos/owner/demo/collaborators/author/permission":
			fmt.Fprintf(w, `{"user":{"permissions":{"push":%t}}}`, allow)
		default:
			t.Errorf("unexpected request: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()
	client := api{base: srv.URL, token: "test", client: srv.Client()}
	reg := registration{ID: "12", Source: "owner/demo"}
	for attempt := 0; attempt < 3; attempt++ {
		s, err := inspect(client, reg, "3")
		if err != nil || s.SHA != sha || s.State != "open" || !s.Eligible || s.PR != "3" || s.registration != reg {
			t.Fatalf("attempt %d: state=%+v error=%v", attempt, s, err)
		}
	}
	// An unchanged commit must not reuse an earlier authorization decision.
	allow = false
	if s, err := inspect(client, reg, "3"); err == nil || s.Eligible {
		t.Fatalf("repeat accepted revoked permission: state=%+v error=%v", s, err)
	}
	for _, path := range []string{"/repositories/12", "/repos/owner/demo/pulls/3", "/repos/owner/demo/collaborators/author/permission"} {
		if reads[path] != 4 {
			t.Errorf("%s reads=%d, want 4", path, reads[path])
		}
	}
}

// TestRegistrationRejectsAliases accepts one exact registration and rejects aliases.
func TestRegistrationRejectsAliases(t *testing.T) {
	p := filepath.Join(t.TempDir(), "repositories.json")
	os.WriteFile(p, []byte(`[{"repository_id":"12","source_repository":"owner/demo","port":8080,"source_secret":"SOURCE_DEMO"}]`), 0600)
	if _, err := resolve(p, "12", "owner/demo", "3"); err != nil {
		t.Fatal(err)
	}
	for _, tc := range [][3]string{{"012", "owner/demo", "3"}, {"12", "other/demo", "3"}, {"12", "owner/demo", "03"}, {"13", "owner/demo", "3"}} {
		if _, err := resolve(p, tc[0], tc[1], tc[2]); err == nil {
			t.Fatalf("accepted %v", tc)
		}
	}
}
