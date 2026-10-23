package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"
)

const (
	userDefaultConfigEnv  = "PREVIEWMESH_CONFIG"
	userKubeconfigEnv     = "PREVIEWMESH_KUBECONFIG"
	userIngressPort       = 18080
	userLifecycleMaxAge   = 24 * time.Hour
	userKubernetesTimeout = time.Minute
)

var (
	userNamespacePattern = regexp.MustCompile(`^pm-r([1-9][0-9]*)-pr([1-9][0-9]*)$`)
	userImagePattern     = regexp.MustCompile(`(?:^|/)previewmesh-c([1-9][0-9]*)-r([1-9][0-9]*)(?:@|:)`)
	userDNSLabelPattern  = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`)
)

// userCLIError carries a stable exit class without changing legacy command errors.
type userCLIError struct {
	code int
	err  error
}

func (e *userCLIError) Error() string { return e.err.Error() }

func userError(code int, format string, args ...any) error {
	return &userCLIError{code: code, err: fmt.Errorf(format, args...)}
}

func userCLIExitCode(err error) int {
	var typed *userCLIError
	if errors.As(err, &typed) {
		return typed.code
	}
	return 1
}

// isUserCommand identifies the additive, human-facing command layer.
func isUserCommand(command string) bool {
	switch command {
	case "", "--help", "-h", "help", "ps", "inspect", "health", "rm":
		return true
	default:
		return false
	}
}

type userRegistration struct {
	ID     string `json:"repository_id"`
	Source string `json:"source_repository"`
	Port   int    `json:"port"`
}

type userConfig struct {
	Path             string
	Control          string
	ControlDirectory string
	ControlID        string
	Kubeconfig       string
	RegistryPath     string
	DomainSuffix     string
	Registrations    []userRegistration
}

type userRuntime struct {
	config     userConfig
	run        func(context.Context, string, ...string) ([]byte, error)
	readGitHub func(string, bool) ([]byte, error)
	httpClient *http.Client
	now        func() time.Time
}

type userNamespace struct {
	Metadata struct {
		Name        string            `json:"name"`
		UID         string            `json:"uid"`
		Labels      map[string]string `json:"labels"`
		Annotations map[string]string `json:"annotations"`
	} `json:"metadata"`
}

type userNamespaceList struct {
	Items []userNamespace `json:"items"`
}

type userResource struct {
	Kind     string `json:"kind"`
	Metadata struct {
		Name string `json:"name"`
	} `json:"metadata"`
	Spec struct {
		Replicas  *int   `json:"replicas"`
		ClusterIP string `json:"clusterIP"`
		Rules     []struct {
			Host string `json:"host"`
		} `json:"rules"`
		Template struct {
			Spec struct {
				Containers []struct {
					Image string `json:"image"`
				} `json:"containers"`
			} `json:"spec"`
		} `json:"template"`
	} `json:"spec"`
	Status struct {
		Replicas      int    `json:"replicas"`
		ReadyReplicas int    `json:"readyReplicas"`
		Phase         string `json:"phase"`
		Conditions    []struct {
			Type   string `json:"type"`
			Status string `json:"status"`
		} `json:"conditions"`
		ContainerStatuses []struct {
			State struct {
				Waiting *struct {
					Reason string `json:"reason"`
				} `json:"waiting"`
				Terminated *struct {
					Reason string `json:"reason"`
				} `json:"terminated"`
			} `json:"state"`
		} `json:"containerStatuses"`
	} `json:"status"`
}

type userResourceList struct {
	Items []userResource `json:"items"`
}

type userPreview struct {
	Name                 string    `json:"name"`
	Namespace            string    `json:"namespace"`
	SourceRepository     string    `json:"source_repository"`
	RepositoryID         string    `json:"repository_id"`
	PR                   string    `json:"pr"`
	RuntimeState         string    `json:"runtime_state"`
	LifecycleState       string    `json:"lifecycle_state"`
	Control              string    `json:"control"`
	DesiredReplicas      int       `json:"desired_replicas"`
	ReadyReplicas        int       `json:"ready_replicas"`
	URL                  string    `json:"url,omitempty"`
	VerifiedSHA          string    `json:"verified_sha,omitempty"`
	RuntimeDeployment    string    `json:"runtime_deployment,omitempty"`
	RuntimePods          string    `json:"runtime_pods,omitempty"`
	RuntimeService       string    `json:"runtime_service,omitempty"`
	RuntimeIngress       string    `json:"runtime_ingress,omitempty"`
	LifecyclePRState     string    `json:"lifecycle_pr_state,omitempty"`
	LifecycleSHA         string    `json:"lifecycle_sha,omitempty"`
	LifecycleBuild       string    `json:"lifecycle_build,omitempty"`
	LifecycleDeployment  string    `json:"lifecycle_deployment,omitempty"`
	LifecycleReadiness   string    `json:"lifecycle_readiness,omitempty"`
	LifecycleHealth      string    `json:"lifecycle_health,omitempty"`
	LifecycleCleanup     string    `json:"lifecycle_cleanup,omitempty"`
	EvidenceURL          string    `json:"evidence_url,omitempty"`
	ReportedURL          string    `json:"reported_url,omitempty"`
	WorkflowURL          string    `json:"workflow_url,omitempty"`
	LifecycleExplanation string    `json:"lifecycle_explanation,omitempty"`
	Error                string    `json:"error,omitempty"`
	ObservedAt           time.Time `json:"observed_at"`
}

type userTarget struct {
	Namespace    string
	RepositoryID string
	PR           string
	Source       string
	Registration *userRegistration
}

type userHealth struct {
	URL        string `json:"url"`
	HTTPStatus int    `json:"http_status"`
	Status     string `json:"status,omitempty"`
	CommitSHA  string `json:"commit_sha,omitempty"`
	Healthy    bool   `json:"healthy"`
	Error      string `json:"error,omitempty"`
}

type userState struct {
	Status  string `json:"state"`
	Merged  bool   `json:"merged"`
	HeadSHA string `json:"sha"`
}

// runUserCLI is the public-facing dispatcher. Legacy commands bypass it entirely.
func runUserCLI(args []string, out, errOut io.Writer, input io.Reader) error {
	if len(args) == 0 || args[0] == "--help" || args[0] == "-h" {
		return printUserHelp(out, "")
	}
	switch args[0] {
	case "help":
		if len(args) > 2 {
			return userError(2, "usage: previewmesh help [COMMAND]")
		}
		command := ""
		if len(args) == 2 {
			command = args[1]
		}
		return printUserHelp(out, command)
	case "ps":
		return runUserPS(args[1:], out, errOut)
	case "inspect":
		return runUserInspect(args[1:], out, errOut)
	case "health":
		return runUserHealth(args[1:], out, errOut)
	case "rm":
		return runUserRM(args[1:], out, errOut, input)
	default:
		return userError(2, "unknown user command %q; run 'previewmesh help'", args[0])
	}
}

func printUserHelp(out io.Writer, command string) error {
	switch command {
	case "", "previewmesh":
		_, err := io.WriteString(out, `PreviewMesh preview manager / 预览管理工具

User commands:
  previewmesh ps [--json] [--all]          list previews / 列出预览
  previewmesh inspect TARGET [--json]      inspect one preview / 查看详情
  previewmesh health TARGET [--json]       probe /health / 检查健康接口
  previewmesh rm TARGET [--yes] [--force]  remove a preview / 删除预览
  previewmesh help [COMMAND]               show help / 显示帮助

TARGET may be a PR number, source#PR, owner/name#PR, or a preview name.
Use --json for automation. The default configuration is read from
~/.config/previewmesh/setup.ini.

Advanced CI commands (kept for existing automation):
  preflight, status, build, deploy, verify, cleanup,
  cleanup-retry, cleanup-inspect

Run 'previewmesh help COMMAND' for command-specific help.
`)
		return err
	case "ps":
		_, err := io.WriteString(out, `Usage: previewmesh ps [--json] [--all] [--config PATH] [--kubeconfig PATH]

By default, list previews for registered sources and exclude resources known
to belong to another control repository. Unknown ownership remains visible
for diagnosis. Use --all to include all managed preview namespaces.
GitHub lifecycle evidence is displayed separately from the live Kubernetes
runtime state.
`)
		return err
	case "inspect":
		_, err := io.WriteString(out, `Usage: previewmesh inspect TARGET [--json] [--config PATH] [--kubeconfig PATH]

Show one preview's Namespace, Deployment, Pods, Service, Ingress, URL, live
runtime state, and the corresponding GitHub lifecycle evidence.
`)
		return err
	case "health":
		_, err := io.WriteString(out, `Usage: previewmesh health TARGET [--json] [--timeout DURATION] [--config PATH] [--kubeconfig PATH]

Send a live GET request to the preview's /health endpoint. A healthy response
does not by itself prove that the PR lifecycle state is ready.
`)
		return err
	case "rm":
		_, err := io.WriteString(out, `Usage: previewmesh rm TARGET [--yes] [--force] [--json] [--config PATH] [--kubeconfig PATH]

Remove one owned preview Namespace. The command asks for confirmation unless
--yes is supplied. An open PR requires --force. This command never changes the
PR itself and never removes a preview belonging to another control repository.
`)
		return err
	case "preflight", "status", "build", "deploy", "verify", "cleanup", "cleanup-retry", "cleanup-inspect":
		_, err := fmt.Fprintf(out, "Advanced command %q is preserved for CI. Run 'previewmesh %s --help' for its existing flags.\n", command, command)
		return err
	default:
		return userError(2, "unknown help topic %q; run 'previewmesh help'", command)
	}
}

func normalizeUserArgs(args []string, valueFlags map[string]bool) ([]string, []string, error) {
	flags := make([]string, 0, len(args))
	positionals := make([]string, 0, 1)
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			positionals = append(positionals, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(arg, "-") || arg == "-" {
			positionals = append(positionals, arg)
			continue
		}
		flags = append(flags, arg)
		name := strings.TrimLeft(arg, "-")
		if strings.Contains(name, "=") {
			continue
		}
		if valueFlags[name] {
			if i+1 >= len(args) {
				return nil, nil, userError(2, "flag %q requires a value", arg)
			}
			i++
			flags = append(flags, args[i])
		}
	}
	return flags, positionals, nil
}

func userFlagSet(name string, out io.Writer) *flag.FlagSet {
	f := flag.NewFlagSet(name, flag.ContinueOnError)
	f.SetOutput(io.Discard)
	f.Usage = func() { _ = printUserHelp(out, name) }
	return f
}

func addUserConfigFlags(f *flag.FlagSet, configPath, kubeconfig *string) {
	f.StringVar(configPath, "config", "", "PreviewMesh setup.ini path")
	f.StringVar(kubeconfig, "kubeconfig", "", "Runner kubeconfig path")
}

func runUserPS(args []string, out, errOut io.Writer) error {
	flags, positionals, err := normalizeUserArgs(args, map[string]bool{"config": true, "kubeconfig": true})
	if err != nil {
		return err
	}
	f := userFlagSet("ps", out)
	asJSON := f.Bool("json", false, "print JSON")
	all := f.Bool("all", false, "include foreign and unknown control resources")
	var configPath, kubeconfig string
	addUserConfigFlags(f, &configPath, &kubeconfig)
	if err := f.Parse(flags); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return userError(2, "%v", err)
	}
	if len(positionals) != 0 {
		return userError(2, "usage: previewmesh ps [--json] [--all]")
	}
	cfg, err := loadUserConfig(configPath, kubeconfig)
	if err != nil {
		return userError(1, "%v", err)
	}
	r := newUserRuntime(cfg)
	previews, err := r.listPreviews(context.Background(), *all)
	if err != nil {
		return userError(1, "%v", err)
	}
	for i := range previews {
		r.addLifecycle(&previews[i])
	}
	if *asJSON {
		return json.NewEncoder(out).Encode(previews)
	}
	printPreviewTable(out, previews)
	return nil
}

func runUserInspect(args []string, out, errOut io.Writer) error {
	flags, positionals, err := normalizeUserArgs(args, map[string]bool{"config": true, "kubeconfig": true})
	if err != nil {
		return err
	}
	f := userFlagSet("inspect", out)
	asJSON := f.Bool("json", false, "print JSON")
	var configPath, kubeconfig string
	addUserConfigFlags(f, &configPath, &kubeconfig)
	if err := f.Parse(flags); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return userError(2, "%v", err)
	}
	if len(positionals) != 1 {
		return userError(2, "usage: previewmesh inspect TARGET [--json]")
	}
	cfg, err := loadUserConfig(configPath, kubeconfig)
	if err != nil {
		return userError(1, "%v", err)
	}
	r := newUserRuntime(cfg)
	target, ns, err := r.resolveTarget(context.Background(), positionals[0])
	if err != nil {
		return userError(2, "%v", err)
	}
	if ns == nil {
		return userError(1, "preview %q is not running", positionals[0])
	}
	preview, err := r.previewFromNamespace(context.Background(), *ns, target.Registration)
	if err != nil {
		return userError(1, "%v", err)
	}
	r.addLifecycle(&preview)
	if *asJSON {
		return json.NewEncoder(out).Encode(preview)
	}
	printPreviewDetails(out, preview)
	return nil
}

func runUserHealth(args []string, out, errOut io.Writer) error {
	flags, positionals, err := normalizeUserArgs(args, map[string]bool{"config": true, "kubeconfig": true, "timeout": true})
	if err != nil {
		return err
	}
	f := userFlagSet("health", out)
	asJSON := f.Bool("json", false, "print JSON")
	timeout := f.Duration("timeout", 10*time.Second, "HTTP timeout")
	var configPath, kubeconfig string
	addUserConfigFlags(f, &configPath, &kubeconfig)
	if err := f.Parse(flags); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return userError(2, "%v", err)
	}
	if len(positionals) != 1 || *timeout <= 0 {
		return userError(2, "usage: previewmesh health TARGET [--json] [--timeout DURATION]")
	}
	cfg, err := loadUserConfig(configPath, kubeconfig)
	if err != nil {
		return userError(1, "%v", err)
	}
	r := newUserRuntime(cfg)
	target, ns, err := r.resolveTarget(context.Background(), positionals[0])
	if err != nil {
		return userError(2, "%v", err)
	}
	if ns == nil {
		return userError(1, "preview %q is not running", positionals[0])
	}
	preview, err := r.previewFromNamespace(context.Background(), *ns, target.Registration)
	if err != nil {
		return userError(1, "%v", err)
	}
	if preview.URL == "" {
		return userError(1, "preview %q has no Ingress URL", positionals[0])
	}
	health := r.probeHealth(context.Background(), preview.URL, *timeout)
	if *asJSON {
		if err := json.NewEncoder(out).Encode(health); err != nil {
			return err
		}
	} else {
		printHealth(out, health)
	}
	if !health.Healthy {
		return userError(1, "preview health check failed: %s", health.Error)
	}
	return nil
}

func runUserRM(args []string, out, errOut io.Writer, input io.Reader) error {
	flags, positionals, err := normalizeUserArgs(args, map[string]bool{"config": true, "kubeconfig": true})
	if err != nil {
		return err
	}
	f := userFlagSet("rm", out)
	asJSON := f.Bool("json", false, "print JSON")
	yes := f.Bool("yes", false, "skip confirmation")
	force := f.Bool("force", false, "allow removal while PR is open")
	var configPath, kubeconfig string
	addUserConfigFlags(f, &configPath, &kubeconfig)
	if err := f.Parse(flags); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return userError(2, "%v", err)
	}
	if len(positionals) != 1 {
		return userError(2, "usage: previewmesh rm TARGET [--yes] [--force] [--json]")
	}
	cfg, err := loadUserConfig(configPath, kubeconfig)
	if err != nil {
		return userError(1, "%v", err)
	}
	r := newUserRuntime(cfg)
	target, ns, err := r.resolveTarget(context.Background(), positionals[0])
	if err != nil {
		return userError(2, "%v", err)
	}
	if ns == nil {
		result := map[string]string{"target": target.Namespace, "state": "removed", "message": "already absent"}
		if *asJSON {
			return json.NewEncoder(out).Encode(result)
		}
		fmt.Fprintf(out, "Preview %s is already removed.\n", target.Namespace)
		return nil
	}
	preview, err := r.previewFromNamespace(context.Background(), *ns, target.Registration)
	if err != nil {
		return userError(1, "%v", err)
	}
	if preview.Control != "current" {
		return userError(2, "refusing to remove preview %s: control ownership is %s", target.Namespace, preview.Control)
	}
	state, err := r.pullRequestState(target.Source, target.PR)
	if err != nil {
		return userError(1, "cannot verify PR state before removal: %v", err)
	}
	if state.Status != "open" && state.Status != "closed" {
		return userError(1, "cannot remove preview: GitHub returned unsupported PR state %q", state.Status)
	}
	if state.Status == "open" && !*force {
		return userError(2, "PR %s is still open; use --force to remove the preview", target.PR)
	}
	if !*yes {
		promptOut := out
		if *asJSON {
			// Keep machine-readable output separate from interactive confirmation.
			promptOut = errOut
		}
		confirmed, err := confirmRemoval(input, promptOut, target.Namespace)
		if err != nil {
			return userError(2, "%v", err)
		}
		if !confirmed {
			return userError(2, "removal cancelled")
		}
	}
	removed, err := r.removePreview(target, *ns)
	if *asJSON {
		if encodeErr := json.NewEncoder(out).Encode(removed); encodeErr != nil {
			return errors.Join(err, encodeErr)
		}
	} else if err == nil {
		fmt.Fprintf(out, "Removed preview %s.\n", target.Namespace)
	}
	if err != nil {
		return userError(1, "%v", err)
	}
	return nil
}

func confirmRemoval(input io.Reader, out io.Writer, namespace string) (bool, error) {
	if input == nil {
		return false, errors.New("confirmation input is unavailable; use --yes")
	}
	if _, err := fmt.Fprintf(out, "Remove preview %s? [y/N] ", namespace); err != nil {
		return false, err
	}
	reader := bufio.NewReader(input)
	answer, err := reader.ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return false, err
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes", nil
}

func printPreviewTable(out io.Writer, previews []userPreview) {
	// Keep an empty result readable without an otherwise unused table header.
	if len(previews) == 0 {
		fmt.Fprintln(out, "(no previews)")
		fmt.Fprintln(out, "Open a pull request in a registered application repository to create a preview.")
		return
	}
	w := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "NAME\tSOURCE\tPR\tRUNTIME\tLIFECYCLE\tCONTROL\tREADY\tURL\tSHA")
	for _, preview := range previews {
		urlText := preview.URL
		if urlText == "" {
			urlText = "-"
		}
		sha := preview.VerifiedSHA
		if sha == "" {
			sha = "-"
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%d/%d\t%s\t%s\n",
			userDisplay(preview.Name), userDisplay(preview.SourceRepository), userDisplay(preview.PR),
			userDisplay(preview.RuntimeState), userDisplay(preview.LifecycleState), userDisplay(preview.Control), preview.ReadyReplicas,
			preview.DesiredReplicas, userDisplay(urlText), userDisplay(sha))
	}
	_ = w.Flush()
}

func printPreviewDetails(out io.Writer, preview userPreview) {
	for _, row := range [][2]string{
		{"Name", preview.Name},
		{"Source", preview.SourceRepository},
		{"PR", preview.PR},
		{"Runtime state", preview.RuntimeState},
		{"Lifecycle state", preview.LifecycleState},
		{"Control", preview.Control},
		{"Deployment", preview.RuntimeDeployment},
		{"Pods", preview.RuntimePods},
		{"Service", preview.RuntimeService},
		{"Ingress", preview.RuntimeIngress},
		{"URL", preview.URL},
		{"Verified SHA", preview.VerifiedSHA},
		{"Lifecycle PR state", preview.LifecyclePRState},
		{"Lifecycle SHA", preview.LifecycleSHA},
		{"Lifecycle build", preview.LifecycleBuild},
		{"Lifecycle deployment", preview.LifecycleDeployment},
		{"Lifecycle readiness", preview.LifecycleReadiness},
		{"Lifecycle health", preview.LifecycleHealth},
		{"Lifecycle cleanup", preview.LifecycleCleanup},
		{"Reported URL", preview.ReportedURL},
		{"Evidence", preview.EvidenceURL},
		{"Workflow", preview.WorkflowURL},
	} {
		if row[1] != "" {
			fmt.Fprintf(out, "%s: %s\n", row[0], userDisplay(row[1]))
		}
	}
	if preview.LifecycleExplanation != "" {
		fmt.Fprintln(out, "Lifecycle detail:", userDisplay(preview.LifecycleExplanation))
	}
	if preview.Error != "" {
		fmt.Fprintln(out, "Runtime error:", userDisplay(preview.Error))
	}
	if !preview.ObservedAt.IsZero() {
		fmt.Fprintln(out, "Observed at:", preview.ObservedAt.UTC().Format(time.RFC3339))
	}
}

func printHealth(out io.Writer, health userHealth) {
	fmt.Fprintln(out, "URL:", userDisplay(health.URL))
	fmt.Fprintln(out, "HTTP status:", health.HTTPStatus)
	fmt.Fprintln(out, "Application status:", userDisplay(health.Status))
	// Health responses are application-controlled and must remain plain terminal text.
	fmt.Fprintln(out, "Commit SHA:", userDisplay(health.CommitSHA))
	fmt.Fprintln(out, "Healthy:", health.Healthy)
	if health.Error != "" {
		fmt.Fprintln(out, "Error:", userDisplay(health.Error))
	}
}

func userDisplay(value string) string {
	return strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return '?'
		}
		return r
	}, value)
}

// loadUserConfig resolves only local configuration references; it never reads token contents.
func loadUserConfig(configPath, kubeconfigOverride string) (userConfig, error) {
	path := configPath
	if path == "" {
		path = os.Getenv(userDefaultConfigEnv)
	}
	if path == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return userConfig{}, errors.New("cannot determine home directory")
		}
		path = filepath.Join(home, ".config", "previewmesh", "setup.ini")
	}
	path, err := filepath.Abs(filepath.Clean(path))
	if err != nil {
		return userConfig{}, fmt.Errorf("invalid config path: %w", err)
	}
	sections, err := readUserINI(path)
	if err != nil {
		return userConfig{}, fmt.Errorf("read PreviewMesh configuration %s: %w", path, err)
	}
	project := sections["project"]
	control := strings.TrimSpace(project["control_repository"])
	controlDirectory := strings.TrimSpace(project["control_directory"])
	if !sourcePattern.MatchString(control) || controlDirectory == "" {
		return userConfig{}, errors.New("PreviewMesh setup.ini needs project.control_repository and project.control_directory")
	}
	if !filepath.IsAbs(controlDirectory) {
		controlDirectory = filepath.Join(filepath.Dir(path), controlDirectory)
	}
	controlDirectory, err = filepath.Abs(filepath.Clean(controlDirectory))
	if err != nil {
		return userConfig{}, fmt.Errorf("invalid control directory: %w", err)
	}
	network := sections["network"]
	domainSuffix := strings.TrimSpace(network["domain_suffix"])
	if domainSuffix == "" || domainSuffix == "auto" {
		lanIP := strings.TrimSpace(network["lan_ip"])
		if lanIP == "" {
			return userConfig{}, errors.New("PreviewMesh setup.ini needs network.lan_ip when domain_suffix is auto")
		}
		domainSuffix = lanIP + ".sslip.io"
	}
	if err := validateUserDomainSuffix(domainSuffix); err != nil {
		return userConfig{}, err
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return userConfig{}, errors.New("cannot determine home directory")
	}
	controlID := ""
	statePath := filepath.Join(home, ".local", "share", "previewmesh", "install-state.json")
	if data, readErr := os.ReadFile(statePath); readErr == nil {
		var state struct {
			Control   string          `json:"control"`
			ControlID json.RawMessage `json:"control_id"`
		}
		if json.Unmarshal(data, &state) != nil {
			return userConfig{}, fmt.Errorf("invalid PreviewMesh install state: %s", statePath)
		}
		if state.Control != "" && state.Control != control {
			return userConfig{}, fmt.Errorf("installed control repository is %s, but setup.ini selects %s", state.Control, control)
		}
		controlID = stringFromJSONNumber(state.ControlID)
		if controlID != "" && !decimal.MatchString(controlID) {
			return userConfig{}, fmt.Errorf("installed control ID is invalid: %s", statePath)
		}
	}

	registryPath := filepath.Join(controlDirectory, "config", "repositories.json")
	data, err := os.ReadFile(registryPath)
	if err != nil {
		return userConfig{}, fmt.Errorf("read control repository registry %s: %w", registryPath, err)
	}
	var registrations []userRegistration
	if err := json.Unmarshal(data, &registrations); err != nil || len(registrations) == 0 {
		return userConfig{}, fmt.Errorf("control repository registry %s is invalid or empty", registryPath)
	}
	seenIDs := map[string]bool{}
	seenSources := map[string]bool{}
	for _, registration := range registrations {
		if !decimal.MatchString(registration.ID) || !sourcePattern.MatchString(registration.Source) || registration.Port < 1 || registration.Port > 65535 {
			return userConfig{}, fmt.Errorf("invalid registration in %s", registryPath)
		}
		if seenIDs[registration.ID] || seenSources[strings.ToLower(registration.Source)] {
			return userConfig{}, fmt.Errorf("duplicate registration identity in %s", registryPath)
		}
		seenIDs[registration.ID] = true
		seenSources[strings.ToLower(registration.Source)] = true
	}
	kubeconfig := kubeconfigOverride
	if kubeconfig == "" {
		kubeconfig = os.Getenv(userKubeconfigEnv)
	}
	if kubeconfig == "" {
		kubeconfig = filepath.Join(controlDirectory, "config", "previewmesh-runner.yaml")
	}
	kubeconfig, err = filepath.Abs(filepath.Clean(kubeconfig))
	if err != nil {
		return userConfig{}, fmt.Errorf("invalid kubeconfig path: %w", err)
	}
	return userConfig{Path: path, Control: control, ControlDirectory: controlDirectory, ControlID: controlID,
		Kubeconfig: kubeconfig, RegistryPath: registryPath, DomainSuffix: domainSuffix,
		Registrations: registrations}, nil
}

func readUserINI(path string) (map[string]map[string]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	sections := map[string]map[string]string{}
	section := ""
	scanner := bufio.NewScanner(file)
	line := 0
	for scanner.Scan() {
		line++
		text := strings.TrimSpace(scanner.Text())
		if text == "" || strings.HasPrefix(text, ";") || strings.HasPrefix(text, "#") {
			continue
		}
		if strings.HasPrefix(text, "[") && strings.HasSuffix(text, "]") {
			section = strings.TrimSpace(text[1 : len(text)-1])
			if section == "" {
				return nil, fmt.Errorf("empty section at line %d", line)
			}
			if _, exists := sections[section]; exists {
				return nil, fmt.Errorf("duplicate section %q", section)
			}
			sections[section] = map[string]string{}
			continue
		}
		if section == "" {
			return nil, fmt.Errorf("key before section at line %d", line)
		}
		parts := strings.SplitN(text, "=", 2)
		if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" {
			return nil, fmt.Errorf("invalid setting at line %d", line)
		}
		key := strings.TrimSpace(parts[0])
		if _, exists := sections[section][key]; exists {
			return nil, fmt.Errorf("duplicate setting %q", key)
		}
		sections[section][key] = strings.TrimSpace(parts[1])
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return sections, nil
}

func validateUserDomainSuffix(value string) error {
	if len(value) > 199 {
		return errors.New("configured preview domain suffix is too long")
	}
	for _, label := range strings.Split(value, ".") {
		if len(label) > 63 || !userDNSLabelPattern.MatchString(label) {
			return errors.New("configured preview domain suffix is not a valid DNS suffix")
		}
	}
	return nil
}

func stringFromJSONNumber(raw json.RawMessage) string {
	value := strings.TrimSpace(string(raw))
	if value == "" || value == "null" {
		return ""
	}
	if len(value) > 1 && value[0] == '"' {
		var text string
		if json.Unmarshal(raw, &text) == nil {
			return text
		}
	}
	return value
}

func newUserRuntime(config userConfig) *userRuntime {
	return &userRuntime{config: config, run: runUserExternal, readGitHub: githubRead,
		httpClient: &http.Client{}, now: time.Now}
}

func runUserExternal(ctx context.Context, command string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, command, args...)
	var output strings.Builder
	cmd.Stdout = &output
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return nil, fmt.Errorf("%s timed out", command)
		}
		return nil, fmt.Errorf("%s failed", command)
	}
	return []byte(output.String()), nil
}

func (r *userRuntime) kubectl(ctx context.Context, args ...string) ([]byte, error) {
	if r.config.Kubeconfig == "" {
		return nil, errors.New("Runner kubeconfig is not configured")
	}
	// An unreachable cluster must not leave an interactive command waiting forever.
	ctx, cancel := context.WithTimeout(ctx, userKubernetesTimeout)
	defer cancel()
	return r.run(ctx, "kubectl", append([]string{"--kubeconfig", r.config.Kubeconfig}, args...)...)
}

func (r *userRuntime) listNamespaces(ctx context.Context) ([]userNamespace, error) {
	data, err := r.kubectl(ctx, "get", "namespaces", "-l", domain+"managed-by=previewmesh", "-o", "json")
	if err != nil {
		return nil, fmt.Errorf("list PreviewMesh namespaces: %w", err)
	}
	var list userNamespaceList
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, fmt.Errorf("invalid Namespace response: %w", err)
	}
	return list.Items, nil
}

func (r *userRuntime) getNamespace(ctx context.Context, name string) (*userNamespace, error) {
	data, err := r.kubectl(ctx, "get", "namespace", name, "--ignore-not-found", "-o", "json")
	if err != nil {
		return nil, fmt.Errorf("read Namespace %s: %w", name, err)
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return nil, nil
	}
	var namespace userNamespace
	if err := json.Unmarshal(data, &namespace); err != nil {
		return nil, fmt.Errorf("invalid Namespace %s response: %w", name, err)
	}
	return &namespace, nil
}

func (r *userRuntime) getResources(ctx context.Context, namespace string) (userResourceList, error) {
	data, err := r.kubectl(ctx, "get", "deployment,service,ingress,pods", "-n", namespace, "-o", "json")
	if err != nil {
		return userResourceList{}, fmt.Errorf("read runtime resources in %s: %w", namespace, err)
	}
	var list userResourceList
	if err := json.Unmarshal(data, &list); err != nil {
		return userResourceList{}, fmt.Errorf("invalid runtime response in %s: %w", namespace, err)
	}
	return list, nil
}

// listPreviews inventories namespaces first, then enriches each one with bounded runtime data.
func (r *userRuntime) listPreviews(ctx context.Context, all bool) ([]userPreview, error) {
	namespaces, err := r.listNamespaces(ctx)
	if err != nil {
		return nil, err
	}
	previews := make([]userPreview, 0, len(namespaces))
	for _, namespace := range namespaces {
		registration := r.registrationForNamespace(namespace)
		if !all && registration == nil {
			continue
		}
		preview, viewErr := r.previewFromNamespace(ctx, namespace, registration)
		if viewErr != nil {
			preview.Error = viewErr.Error()
		}
		if !all && preview.Control == "foreign" {
			continue
		}
		previews = append(previews, preview)
	}
	sort.Slice(previews, func(i, j int) bool { return previews[i].Name < previews[j].Name })
	return previews, nil
}

func (r *userRuntime) registrationForNamespace(namespace userNamespace) *userRegistration {
	id := namespace.Metadata.Labels[domain+"repository-id"]
	source := namespace.Metadata.Annotations[domain+"source-repository"]
	for i := range r.config.Registrations {
		registration := &r.config.Registrations[i]
		if registration.ID == id && registration.Source == source {
			return registration
		}
	}
	return nil
}

func (r *userRuntime) previewFromNamespace(ctx context.Context, namespace userNamespace, registration *userRegistration) (userPreview, error) {
	repoID := namespace.Metadata.Labels[domain+"repository-id"]
	pr := namespace.Metadata.Labels[domain+"pr-number"]
	source := namespace.Metadata.Annotations[domain+"source-repository"]
	if registration != nil {
		repoID = registration.ID
		source = registration.Source
	}
	preview := userPreview{Name: namespace.Metadata.Name, Namespace: namespace.Metadata.Name, SourceRepository: source,
		RepositoryID: repoID, PR: pr, LifecycleState: "unknown", Control: "unknown", VerifiedSHA: namespace.Metadata.Annotations[domain+"verified-sha"], ObservedAt: r.now()}
	resources, err := r.getResources(ctx, namespace.Metadata.Name)
	if err != nil {
		preview.RuntimeState = "unknown"
		return preview, err
	}
	var deployment, service, ingress *userResource
	var pods []userResource
	// A preview has one release; ambiguous resources cannot establish its ownership.
	for i := range resources.Items {
		item := &resources.Items[i]
		switch strings.ToLower(item.Kind) {
		case "deployment":
			if deployment != nil {
				preview.RuntimeState = "unknown"
				return preview, errors.New("ambiguous runtime resources: more than one Deployment in the preview Namespace")
			}
			deployment = item
		case "service":
			if service != nil {
				preview.RuntimeState = "unknown"
				return preview, errors.New("ambiguous runtime resources: more than one Service in the preview Namespace")
			}
			service = item
		case "ingress":
			if ingress != nil {
				preview.RuntimeState = "unknown"
				return preview, errors.New("ambiguous runtime resources: more than one Ingress in the preview Namespace")
			}
			ingress = item
		case "pod":
			pods = append(pods, *item)
		}
	}
	desired := 1
	ready := 0
	image := ""
	if deployment != nil {
		if deployment.Spec.Replicas != nil {
			desired = *deployment.Spec.Replicas
		}
		ready = deployment.Status.ReadyReplicas
		if len(deployment.Spec.Template.Spec.Containers) > 0 {
			image = deployment.Spec.Template.Spec.Containers[0].Image
		}
		preview.RuntimeDeployment = fmt.Sprintf("ready %d/%d", ready, desired)
	} else {
		preview.RuntimeDeployment = "unavailable"
	}
	preview.DesiredReplicas, preview.ReadyReplicas = desired, ready
	preview.RuntimePods = summarizeUserPods(pods)
	if service != nil {
		preview.RuntimeService = "present"
		if service.Spec.ClusterIP != "" {
			preview.RuntimeService = "present; ClusterIP " + service.Spec.ClusterIP
		}
	} else {
		preview.RuntimeService = "unavailable"
	}
	// Only advertise a route actually observed in the Ingress, never a guessed host.
	if ingress != nil && len(ingress.Spec.Rules) > 0 {
		hosts := make([]string, 0, len(ingress.Spec.Rules))
		for _, rule := range ingress.Spec.Rules {
			if rule.Host != "" {
				hosts = append(hosts, rule.Host)
			}
		}
		preview.RuntimeIngress = "present; hosts " + strings.Join(hosts, ", ")
		if len(hosts) > 0 {
			preview.URL = "http://" + hosts[0] + ":" + strconv.Itoa(userIngressPort)
		}
	} else {
		preview.RuntimeIngress = "unavailable"
	}
	preview.Control = r.controlMatch(image, repoID)
	preview.RuntimeState = classifyUserRuntime(desired, ready, pods, deployment != nil)
	return preview, nil
}

func summarizeUserPods(pods []userResource) string {
	if len(pods) == 0 {
		return "no matching pods"
	}
	ready := 0
	phases := map[string]int{}
	reasons := map[string]bool{}
	for _, pod := range pods {
		phases[pod.Status.Phase]++
		for _, condition := range pod.Status.Conditions {
			if condition.Type == "Ready" && condition.Status == "True" {
				ready++
			}
		}
		for _, container := range pod.Status.ContainerStatuses {
			if container.State.Waiting != nil {
				reasons[container.State.Waiting.Reason] = true
			}
			if container.State.Terminated != nil {
				reasons[container.State.Terminated.Reason] = true
			}
		}
	}
	parts := []string{fmt.Sprintf("ready %d/%d", ready, len(pods))}
	for _, phase := range []string{"Pending", "Running", "Succeeded", "Failed", "Unknown"} {
		if phases[phase] > 0 {
			parts = append(parts, fmt.Sprintf("%s %d", phase, phases[phase]))
		}
	}
	for _, reason := range []string{"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "ContainerCreating", "OOMKilled", "Error"} {
		if reasons[reason] {
			parts = append(parts, reason)
		}
	}
	return strings.Join(parts, "; ")
}

func classifyUserRuntime(desired, ready int, pods []userResource, deploymentPresent bool) string {
	if !deploymentPresent {
		return "unknown"
	}
	if desired == 0 {
		return "stopped"
	}
	for _, pod := range pods {
		for _, container := range pod.Status.ContainerStatuses {
			if container.State.Waiting != nil {
				switch container.State.Waiting.Reason {
				case "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled", "Error":
					return "failed"
				}
			}
		}
	}
	if ready >= desired {
		return "running"
	}
	return "starting"
}

func (r *userRuntime) controlMatch(image, repoID string) string {
	match := userImagePattern.FindStringSubmatch(image)
	if len(match) != 3 || match[2] != repoID || r.config.ControlID == "" {
		return "unknown"
	}
	if match[1] == r.config.ControlID {
		return "current"
	}
	return "foreign"
}

func (r *userRuntime) addLifecycle(preview *userPreview) {
	if preview.SourceRepository == "" || preview.PR == "" || preview.Control == "foreign" {
		return
	}
	status, err := loadPreviewStatus(r.readGitHub, preview.SourceRepository, preview.PR, r.now().UTC(), userLifecycleMaxAge)
	if err != nil {
		preview.LifecycleState = "unknown"
		preview.LifecycleExplanation = err.Error()
		return
	}
	preview.LifecycleState = status.State
	preview.LifecyclePRState = status.PRState
	preview.LifecycleSHA = status.SHA
	preview.LifecycleBuild = status.Build
	preview.LifecycleDeployment = status.Deployment
	preview.LifecycleReadiness = status.Readiness
	preview.LifecycleHealth = status.Health
	preview.LifecycleCleanup = status.Cleanup
	preview.EvidenceURL = status.EvidenceURL
	preview.ReportedURL = status.URL
	preview.WorkflowURL = status.RunURL
	preview.LifecycleExplanation = status.Explanation
}

// resolveTarget maps friendly input to one registered repository/PR identity before mutation.
func (r *userRuntime) resolveTarget(ctx context.Context, raw string) (userTarget, *userNamespace, error) {
	if strings.TrimSpace(raw) == "" || strings.ContainsAny(raw, " \t\r\n") {
		return userTarget{}, nil, errors.New("TARGET must be a PR number, source#PR, owner/name#PR, or preview name")
	}
	var repoID, pr, source string
	if match := userNamespacePattern.FindStringSubmatch(raw); len(match) == 3 {
		repoID, pr = match[1], match[2]
	} else if decimal.MatchString(raw) {
		pr = raw
	} else if strings.Count(raw, "#") == 1 {
		parts := strings.SplitN(raw, "#", 2)
		source, pr = parts[0], parts[1]
		if !decimal.MatchString(pr) {
			return userTarget{}, nil, errors.New("TARGET PR must be a positive decimal number")
		}
	} else {
		return userTarget{}, nil, fmt.Errorf("invalid TARGET %q", raw)
	}

	candidates := make([]*userRegistration, 0, len(r.config.Registrations))
	for i := range r.config.Registrations {
		registration := &r.config.Registrations[i]
		if repoID != "" && registration.ID != repoID {
			continue
		}
		if source != "" && source != registration.Source && source != filepath.Base(strings.ReplaceAll(registration.Source, "/", string(os.PathSeparator))) {
			continue
		}
		candidates = append(candidates, registration)
	}
	if len(candidates) == 0 {
		return userTarget{}, nil, fmt.Errorf("TARGET %q does not match a source registered by the current control repository", raw)
	}
	var found userTarget
	var foundNS *userNamespace
	for _, registration := range candidates {
		name := "pm-r" + registration.ID + "-pr" + pr
		namespace, err := r.getNamespace(ctx, name)
		if err != nil {
			return userTarget{}, nil, err
		}
		if namespace != nil {
			// Match every identity marker before a read or delete can adopt this name.
			labels, annotations := namespace.Metadata.Labels, namespace.Metadata.Annotations
			if labels[domain+"managed-by"] != "previewmesh" || labels[domain+"repository-id"] != registration.ID || labels[domain+"pr-number"] != pr || annotations[domain+"source-repository"] != registration.Source {
				return userTarget{}, nil, fmt.Errorf("preview %s failed Namespace ownership validation", name)
			}
		}
		if namespace == nil && len(candidates) > 1 {
			continue
		}
		if found.Registration != nil {
			return userTarget{}, nil, fmt.Errorf("TARGET %q is ambiguous; use %s#%s or a full preview name", raw, registration.Source, pr)
		}
		found = userTarget{Namespace: name, RepositoryID: registration.ID, PR: pr, Source: registration.Source, Registration: registration}
		foundNS = namespace
	}
	if found.Registration == nil {
		if len(candidates) > 1 {
			choices := make([]string, 0, len(candidates))
			for _, registration := range candidates {
				choices = append(choices, registration.Source+"#"+pr)
			}
			return userTarget{}, nil, fmt.Errorf("TARGET %q is ambiguous; choose one of %s", raw, strings.Join(choices, ", "))
		}
		registration := candidates[0]
		found = userTarget{Namespace: "pm-r" + registration.ID + "-pr" + pr, RepositoryID: registration.ID, PR: pr, Source: registration.Source, Registration: registration}
	}
	return found, foundNS, nil
}

func (r *userRuntime) pullRequestState(source, pr string) (userState, error) {
	data, err := r.readGitHub("repos/"+source+"/pulls/"+pr, false)
	if err != nil {
		return userState{}, err
	}
	var response struct {
		Number uint64 `json:"number"`
		State  string `json:"state"`
		Merged bool   `json:"merged"`
		Head   struct {
			SHA string `json:"sha"`
		} `json:"head"`
	}
	if err := json.Unmarshal(data, &response); err != nil || strconv.FormatUint(response.Number, 10) != pr {
		return userState{}, errors.New("GitHub returned invalid pull request data")
	}
	if response.Merged {
		response.State = "closed"
	}
	return userState{Status: response.State, Merged: response.Merged, HeadSHA: response.Head.SHA}, nil
}

// probeHealth performs one bounded live request and reports health separately from lifecycle evidence.
func (r *userRuntime) probeHealth(parent context.Context, previewURL string, timeout time.Duration) userHealth {
	health := userHealth{URL: strings.TrimRight(previewURL, "/")}
	parsed, err := url.Parse(health.URL)
	if err != nil || parsed.Hostname() == "" || parsed.User != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		health.Error = "preview URL is invalid"
		return health
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, health.URL+"/health", nil)
	if err != nil {
		health.Error = "cannot create health request"
		return health
	}
	// Redirects can lead to another application and must not establish preview health.
	client := *r.httpClient
	client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	response, err := client.Do(request)
	if err != nil {
		health.Error = "HTTP request failed"
		return health
	}
	defer response.Body.Close()
	health.HTTPStatus = response.StatusCode
	if response.StatusCode != http.StatusOK {
		health.Error = fmt.Sprintf("health endpoint returned HTTP %d; expected HTTP 200 with status ok", response.StatusCode)
		return health
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 64<<10))
	if err != nil {
		health.Error = "cannot read health response"
		return health
	}
	var payload struct {
		Status    string `json:"status"`
		CommitSHA string `json:"commit_sha"`
	}
	if json.Unmarshal(body, &payload) != nil {
		health.Error = "health response was not valid JSON"
		return health
	}
	health.Status, health.CommitSHA = payload.Status, payload.CommitSHA
	health.Healthy = response.StatusCode == http.StatusOK && payload.Status == "ok" && shaPattern.MatchString(payload.CommitSHA)
	if !health.Healthy {
		health.Error = "health response did not report status ok with a valid commit SHA"
	}
	return health
}

// removePreview delegates deletion to the existing UID-bound cleanup implementation.
func (r *userRuntime) removePreview(target userTarget, namespace userNamespace) (map[string]any, error) {
	options := options{command: "cleanup", repoID: target.RepositoryID, pr: target.PR, source: target.Source,
		domainSuffix: r.config.DomainSuffix, timeout: 5 * time.Minute, httpTimeout: time.Minute,
		kubeconfig: r.config.Kubeconfig, namespaceUID: namespace.Metadata.UID}
	x := runner{o: options, r: result{Namespace: target.Namespace, URL: "http://" + target.Namespace + "." + r.config.DomainSuffix + ":" + strconv.Itoa(userIngressPort)}}
	err := x.cleanup()
	if err != nil && namespace.Metadata.UID != "" {
		retryOptions := options
		retryOptions.command = "cleanup-retry"
		retryOptions.namespaceUID = namespace.Metadata.UID
		retryOptions.cleanupAttempts = 3
		retry := runner{o: retryOptions, r: result{Namespace: target.Namespace}}
		err = retry.retryCleanup()
		x = retry
	}
	result := map[string]any{"target": target.Namespace, "state": "failed", "cleanup": x.r.Cleanup, "namespace": target.Namespace}
	if err == nil && x.r.Cleanup == "confirmed_absent" {
		result["state"] = "removed"
	}
	if err != nil {
		result["error"] = err.Error()
	}
	return result, err
}
