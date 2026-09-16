// Tests cover pull request policy and exact registration matching.
package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestCurrentPRPolicy checks open, closed, forked, and permission-denied PRs.
func TestCurrentPRPolicy(t *testing.T) {
	for _, tc := range []struct {
		name, state string
		head        int
		push        bool
		ok          bool
	}{
		{"write author", "open", 12, true, true}, {"maintainer effective push", "open", 12, true, true},
		{"read author", "open", 12, false, false}, {"fork", "open", 13, true, false},
		{"closed former author", "closed", 12, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			permissionCalls := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				// Return only the API responses needed for this policy case.
				switch r.URL.Path {
				case "/repositories/12":
					fmt.Fprint(w, `{"id":12,"full_name":"owner/demo"}`)
				case "/repos/owner/demo/pulls/3":
					fmt.Fprintf(w, `{"number":3,"state":%q,"head":{"sha":%q,"repo":{"id":%d}},"base":{"repo":{"id":12}},"user":{"login":"author"}}`, tc.state, strings.Repeat("a", 40), tc.head)
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
			if tc.state == "closed" && (s.Eligible || permissionCalls != 0) {
				t.Fatal("cleanup incorrectly requires author access")
			}
		})
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
