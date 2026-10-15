# PreviewMesh Preview Runtime

This context defines the language used when describing a pull-request preview
and its relationship to the live cluster and GitHub evidence.

## Language

**Preview / 预览**:
A temporary application environment associated with one source repository and
pull request. It is represented by one PreviewMesh-managed Kubernetes Namespace.
_Avoid_: container, deployment (when referring to the whole environment)

**Runtime state / 预览运行态**:
The current Kubernetes observation of a Preview, such as running, starting,
stopped, failed, or unknown.
_Avoid_: lifecycle state, PR status

**Lifecycle evidence / 生命周期证据**:
The GitHub pull-request and workflow record that describes what PreviewMesh
last verified for a particular source commit.
_Avoid_: live health, runtime state

**Health / 健康**:
The result of a live HTTP request to a Preview's `/health` endpoint, including
its HTTP status and served commit SHA.
_Avoid_: ready, lifecycle evidence

**Readiness / 就绪**:
The Kubernetes Deployment and Pod readiness observation for a Preview. It is
not an HTTP health result and does not by itself establish that the pull
request lifecycle is ready.
_Avoid_: health, lifecycle state

**Removed / 已删除**:
A terminal resource condition in which the owned Preview Namespace is absent
after a deletion operation has been confirmed.
_Avoid_: stopped, failed

