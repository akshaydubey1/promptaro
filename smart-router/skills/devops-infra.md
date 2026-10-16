---
copyright: "Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE."
name: devops-infra
task_types: [coding, writing, document]
triggers: [terraform, kubernetes, k8s, gke, eks, aks, helm, docker, dockerfile, ci, cd, pipeline, github actions, argo, ansible, gcp, aws, azure]
priority: 2
---
Infrastructure work:
- Pin versions (providers, images, charts). No `latest` tags.
- Least-privilege IAM; never hard-code secrets — reference a secret manager or env var.
- Make changes idempotent and say how to plan/diff before apply.
- Call out blast radius and rollback for anything touching production.
