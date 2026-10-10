# Langfuse text skills

Cloud support skills contain instructions and knowledge in one UTF-8 `SKILL.md`.
Scripts, attachments and supporting files are not part of a skill. Tools used
by the agent are configured separately; executing a sandbox tool still uses
the existing sandbox authorization and lifecycle.

Copy `config.example.yaml` to `config.yaml` as usual. Set these environment
variables on the Gateway (or in the root `.env` used by your launcher):

```dotenv
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://your-langfuse.example.com
```

```yaml
skills:
  use: deerflow.skills.storage.langfuse_skill_storage:LangfuseSkillStorage
  langfuse_prefix: deerflow/skills
  langfuse_label: production
  container_path: /mnt/skills
```

Create a **text** prompt in Langfuse named
`deerflow/skills/public/cloud-troubleshooting`, with the full document as its
prompt content:

```markdown
---
name: cloud-troubleshooting
description: Triage cloud infrastructure incidents and plan safe recovery.
---
Collect the cloud provider, region, affected service, incident timeline,
recent changes and relevant error messages. Never request secret key values.
State the evidence for each hypothesis. Prefer read-only diagnostics and
explain the impact before proposing changes to production resources.
```

Assign the configured `production` label to the version you want to serve.
Skills use literal prompt text; `{{variables}}` are not compiled. Publishing
another version in the Langfuse UI only changes runtime behavior after you
move the serving label. After publishing, call the existing admin endpoint
`POST /api/skills/reload` on each worker to refresh discovery/prompt caches.
Reads and slash activation fetch the serving version directly, without an
SDK stale-cache fallback or sandbox acquisition.

Public prompts are shared and read-only in DeerFlow. Custom prompts use
`deerflow/skills/custom/<safe-user-id>/<skill-name>`; the user ID is resolved
by the backend, never accepted as part of the skill name. Custom edits and
rollbacks publish a new version with the serving label. History metadata is
stored in that version's `config.history`. Deleting a custom skill publishes
a tombstone (`config.deleted: true`); previous versions remain available for
history and rollback. A Langfuse edit without DeerFlow metadata still appears
in history, with its previous text available for rollback.

The settings editor, `skill_manage` create/edit/patch, history, rollback and
custom `.skill` export keep their existing interfaces. Imports accept a ZIP
with only `<skill-name>/SKILL.md`; packages containing anything else fail
validation. `skill_manage` support-file operations are rejected. Local files
under the DeerFlow home's `langfuse-skills/` are disposable parser/export
snapshots, not the source of truth, and are not mounted into sandboxes.

Existing repository skill packages and JSONL history are left untouched and
are not automatically imported. Most bundled skills depend on scripts or
attachments: rewrite them as self-contained cloud-support text before
publishing the document in Langfuse. The original file backend remains
available by explicitly selecting
`deerflow.skills.storage.local_skill_storage:LocalSkillStorage`. The schema's
default remains the file backend for embedded clients that omit skills config.

Langfuse credentials grant the Gateway project-wide access: protect the
Langfuse project and allow only trusted operators to edit public skills.
Remote errors fail the read/save rather than silently using a local copy.
Concurrent writes (including simultaneous creates/imports) create independent
immutable versions; the last label update wins. There is no compare-and-swap
editing lock across Gateway processes.

Langfuse's [version and label semantics](https://langfuse.com/docs/prompt-management/features/prompt-version-control)
also apply when managing prompts directly in its UI.
