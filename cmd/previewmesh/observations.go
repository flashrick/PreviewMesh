package main

import (
	"encoding/json"
	"fmt"
	"strings"
)

// observeRuntime collects bounded, read-only diagnostics without changing the attempt outcome.
func (x *runner) observeRuntime() {
	x.r.Runtime = make(map[string]string)
	for _, kind := range []string{"deployment", "pods", "service", "ingress"} {
		args := []string{"get", kind}
		if kind == "pods" {
			args = append(args, "-l", "app.kubernetes.io/instance="+x.r.Namespace)
		} else {
			args = append(args, x.r.Namespace)
		}
		args = append(args, "-n", x.r.Namespace, "-o", "json", "--request-timeout=5s")
		data, err := x.run(nil, "kubectl", args...)
		if err != nil {
			x.r.Runtime[kind] = "unavailable (resource missing or API request failed)"
			continue
		}
		x.r.Runtime[kind] = summarizeRuntime(kind, data)
	}
}

// summarizeRuntime reports observed fields; existence alone is not a readiness claim.
func summarizeRuntime(kind string, data []byte) string {
	var obj struct {
		Metadata struct {
			Name       string
			Generation int64
		}
		Spec struct {
			Replicas  *int
			ClusterIP string
			Rules     []struct{ Host string }
		}
		Status struct {
			Replicas, ReadyReplicas, UpdatedReplicas, AvailableReplicas int
			ObservedGeneration                                          int64
		}
		Items []struct {
			Status struct {
				Phase             string
				Conditions        []struct{ Type, Status string }
				ContainerStatuses []struct {
					State struct {
						Waiting    *struct{ Reason string }
						Terminated *struct{ Reason string }
					}
				}
			}
		}
	}
	if json.Unmarshal(data, &obj) != nil {
		return "unavailable (invalid API response)"
	}
	if kind != "pods" && obj.Metadata.Name == "" {
		return "unavailable (resource not returned)"
	}
	switch kind {
	case "deployment":
		desired := 1
		if obj.Spec.Replicas != nil {
			desired = *obj.Spec.Replicas
		}
		return fmt.Sprintf("ready %d/%d; updated %d; available %d; observed generation %d/%d", obj.Status.ReadyReplicas, desired, obj.Status.UpdatedReplicas, obj.Status.AvailableReplicas, obj.Status.ObservedGeneration, obj.Metadata.Generation)
	case "pods":
		if len(obj.Items) == 0 {
			return "no matching pods"
		}
		counts := map[string]int{}
		ready := 0
		reasons := map[string]bool{}
		for _, pod := range obj.Items {
			counts[pod.Status.Phase]++
			for _, c := range pod.Status.Conditions {
				if c.Type == "Ready" && c.Status == "True" {
					ready++
				}
			}
			for _, c := range pod.Status.ContainerStatuses {
				if c.State.Waiting != nil {
					reasons[c.State.Waiting.Reason] = true
				}
				if c.State.Terminated != nil {
					reasons[c.State.Terminated.Reason] = true
				}
			}
		}
		text := fmt.Sprintf("ready %d/%d", ready, len(obj.Items))
		for _, phase := range []string{"Pending", "Running", "Succeeded", "Failed", "Unknown"} {
			if counts[phase] > 0 {
				text += fmt.Sprintf("; %s %d", phase, counts[phase])
			}
		}
		// Report fixed reason labels rather than arbitrary application-controlled text.
		for _, reason := range []string{"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "ContainerCreating", "OOMKilled", "Error"} {
			if reasons[reason] {
				text += "; " + reason
			}
		}
		return text
	case "service":
		if obj.Spec.ClusterIP == "" {
			return "present; ClusterIP not assigned"
		}
		return "present; ClusterIP " + obj.Spec.ClusterIP
	case "ingress":
		hosts := []string{}
		for _, rule := range obj.Spec.Rules {
			hosts = append(hosts, rule.Host)
		}
		return "present; hosts " + strings.Join(hosts, ", ")
	}
	return "not checked"
}
