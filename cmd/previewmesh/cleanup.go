package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// Recovery retains each failed attempt even when a later attempt confirms absence.
type cleanupRecovery struct {
	NamespaceUID string           `json:"namespace_uid"`
	Attempts     []cleanupAttempt `json:"attempts"`
}
type cleanupAttempt struct {
	Number  int    `json:"number"`
	Outcome string `json:"outcome"`
	Error   string `json:"error,omitempty"`
}

// Inspection exposes only metadata; Secret contents and application data stay private.
type cleanupResource struct {
	Resource          string   `json:"resource"`
	Name              string   `json:"name"`
	UID               string   `json:"uid"`
	Finalizers        []string `json:"finalizers,omitempty"`
	DeletionTimestamp string   `json:"deletion_timestamp,omitempty"`
}
type cleanupInspection struct {
	NamespaceFinalizers []string          `json:"namespace_finalizers,omitempty"`
	State               string            `json:"state"`
	Scope               string            `json:"scope"`
	NamespaceUID        string            `json:"namespace_uid,omitempty"`
	Remaining           []cleanupResource `json:"remaining"`
	Errors              []string          `json:"errors"`
}

// retryCleanup never adopts a replacement Namespace, including across CLI invocations.
func (x *runner) retryCleanup() error {
	x.r.Recovery = &cleanupRecovery{NamespaceUID: x.o.namespaceUID}
	var lastErr error
	for i := 1; i <= x.o.cleanupAttempts; i++ {
		x.r.FailedStage = ""
		lastErr = x.cleanup()
		attempt := cleanupAttempt{Number: i, Outcome: x.r.Cleanup}
		if lastErr != nil {
			attempt.Error = lastErr.Error()
		}
		x.r.Recovery.Attempts = append(x.r.Recovery.Attempts, attempt)
		if lastErr == nil {
			break
		}
	}
	// Inspect after both successful and exhausted retries to retain an independent snapshot.
	inspectionErr := x.stage("cleanup_inspect", x.inspectCleanup)
	return errors.Join(lastErr, inspectionErr)
}

// inspectCleanup discovers every listable namespaced type, including custom resources.
// Missing permissions or API failures leave explicit evidence gaps, never an empty success.
func (x *runner) inspectCleanup() error {
	report := &cleanupInspection{State: "unknown", Scope: "target_namespace; excludes cluster-scoped and external resources", Remaining: []cleanupResource{}, Errors: []string{}}
	x.r.Inspection = report
	fail := func(err error) error {
		report.Errors = append(report.Errors, err.Error())
		report.State = "incomplete"
		return err
	}
	ns, err := x.getNS()
	if err != nil {
		return fail(err)
	}
	if ns == nil {
		report.State = "confirmed_absent"
		return nil
	}
	report.NamespaceUID = ns.Metadata.UID
	report.NamespaceFinalizers = ns.Spec.Finalizers
	if ns.Metadata.UID == "" {
		return fail(errors.New("namespace UID is missing"))
	}
	if x.o.namespaceUID != "" && ns.Metadata.UID != x.o.namespaceUID {
		return fail(errors.New("namespace UID mismatch"))
	}
	report.State = "remaining"
	report.Remaining = append(report.Remaining, cleanupResource{Resource: "namespaces", Name: x.r.Namespace, UID: ns.Metadata.UID, Finalizers: ns.Metadata.Finalizers})
	if ns.Metadata.DeletionTimestamp != nil {
		report.Remaining[0].DeletionTimestamp = *ns.Metadata.DeletionTimestamp
	}
	data, err := x.run(nil, "kubectl", "api-resources", "--namespaced=true", "--verbs=list", "-o", "name")
	if err != nil {
		return fail(fmt.Errorf("discover namespaced resources: %w", err))
	}
	resources := strings.Fields(string(data))
	if len(resources) == 0 {
		return fail(errors.New("resource discovery returned no types"))
	}
	var failures []error
	for _, resource := range resources {
		// No label selector: unlabeled Pods, Helm history, and finalizer blockers also matter.
		data, err = x.run(nil, "kubectl", "get", resource, "--namespace", x.r.Namespace, "-o", "json")
		if err != nil {
			failures = append(failures, fail(fmt.Errorf("list %s: %w", resource, err)))
			continue
		}
		var list struct {
			Items []struct {
				Metadata struct {
					Name              string   `json:"name"`
					UID               string   `json:"uid"`
					Finalizers        []string `json:"finalizers"`
					DeletionTimestamp string   `json:"deletionTimestamp"`
				} `json:"metadata"`
			} `json:"items"`
		}
		if err = json.Unmarshal(data, &list); err != nil || list.Items == nil {
			failures = append(failures, fail(fmt.Errorf("invalid resource list for %s", resource)))
			continue
		}
		for _, item := range list.Items {
			m := item.Metadata
			report.Remaining = append(report.Remaining, cleanupResource{Resource: resource, Name: m.Name, UID: m.UID, Finalizers: m.Finalizers, DeletionTimestamp: m.DeletionTimestamp})
		}
	}
	// Recheck identity after the inventory: concurrent deletion/recreation invalidates the snapshot.
	remaining, err := x.getNS()
	if err != nil {
		failures = append(failures, fail(err))
	} else if remaining == nil || remaining.Metadata.UID != ns.Metadata.UID {
		failures = append(failures, fail(errors.New("namespace changed during inspection; rerun inspection")))
	}
	return errors.Join(failures...)
}
