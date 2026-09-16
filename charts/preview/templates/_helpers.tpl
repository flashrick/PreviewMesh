{{- define "preview.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "preview.fullname" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "preview.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "preview.selectorLabels" -}}
app.kubernetes.io/name: {{ include "preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
