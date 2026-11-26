{{/* Chart name. */}}
{{- define "llm-cost-guard.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Fully qualified app name. */}}
{{- define "llm-cost-guard.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "llm-cost-guard.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "llm-cost-guard.labels" -}}
helm.sh/chart: {{ include "llm-cost-guard.chart" . }}
{{ include "llm-cost-guard.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: llm-cost-guard
{{- end }}

{{- define "llm-cost-guard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llm-cost-guard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "llm-cost-guard.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "llm-cost-guard.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding API keys: an existing one, or the one this chart creates. */}}
{{- define "llm-cost-guard.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "llm-cost-guard.fullname" . }}
{{- end }}
{{- end }}

{{- define "llm-cost-guard.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{/* Guard rails for settings that can't work together. */}}
{{- define "llm-cost-guard.validate" -}}
{{- if and (gt (int .Values.replicaCount) 1) (not .Values.experimental.allowMultipleReplicas) }}
{{- fail "replicaCount > 1 needs a shared database and cache (planned: Postgres + Redis). The bundled SQLite storage supports one replica. See docs/Roadmap.md." }}
{{- end }}
{{- if and .Values.secrets.create .Values.secrets.existingSecret }}
{{- fail "Set either secrets.create or secrets.existingSecret, not both." }}
{{- end }}
{{- end }}

{{/* The mock provider gets its own name label so the gateway's selectors never match it. */}}
{{- define "llm-cost-guard.mockSelectorLabels" -}}
app.kubernetes.io/name: {{ include "llm-cost-guard.name" . }}-mock
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: mock-provider
{{- end }}
