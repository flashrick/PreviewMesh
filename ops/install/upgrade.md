# Update a private deployment-management checkout

[Back to README](../../README.md#update-and-clean-up) · [中文](upgrade_CN.md)

The guided `init`/`install` flow creates or resumes the private deployment-management checkout. Use that existing checkout for later upgrades; keep it separate from this public source checkout. Choose either the review-branch path or the direct-sync path below for each update; do not run both paths for the same update.

Review-branch path:

1. Enter the private deployment-management checkout, prepare `main`, and confirm that the working tree is clean. Run these commands in order:

   ~~~bash
   # Enter the existing private deployment-management checkout.
   # Prepare the local main branch.
   git switch main
   # This must print nothing before you pull.
   git status --short
   git pull --ff-only origin main
   # This must print nothing before you continue.
   git status --short
   ~~~

   If either `git status --short` command prints anything, stop and review, commit, or stash those changes. The updater requires a clean working tree.

2. Run the default updater to create a review branch:

   ~~~bash
   # Stay in the private deployment-management checkout.
   scripts/update-upstream.sh --remote upstream --ref main
   ~~~

   The default updater fetches the public `main` branch and creates a generated review branch; it does not commit to or push `main`. If the `upstream` remote is missing, the script adds the public PreviewMesh repository automatically. If it reports that the repository is already up to date, there is no branch to validate or publish.

3. After the updater reports a generated branch, get its name and run the control-repository checks while that branch is checked out:

   ~~~bash
   # Stay on the generated review branch.
   git branch --show-current
   # Review the generated changes.
   git diff --stat main...HEAD
   # Run the required control-repository checks.
   git diff --check main...HEAD
   go test ./...
   go vet ./...
   python3 scripts/check-workflow.py
   python3 scripts/check-github-secrets.py
   python3 scripts/check-onboard-source.py
   ~~~

4. After every check passes, run the push and `gh pr create` commands in this order:

   ~~~bash
   # Stay on the generated review branch after validation passes.
   git push --set-upstream origin "$(git branch --show-current)"
   gh pr create \
     --base main \
     --head "$(git branch --show-current)" \
     --title "chore: update PreviewMesh from upstream" \
     --body "Review the upstream PreviewMesh changes before merging."
   ~~~

   If any validation command fails, do not run either the push or `gh pr create` command. Keep the branch local, fix or review the failure, and rerun the checks before publishing it.

   Normally, `gh pr create` identifies the current private control repository from this checkout's GitHub `origin`. If it reports `no default remote repository has been set`, first check whether `origin` is missing or points to a different repository:

   ~~~bash
   git remote get-url origin
   git remote -v
   ~~~

   Confirm that `origin` points to the private control repository target and that `upstream` points to the public PreviewMesh source. If `origin` is missing or incorrect, repair the remote first and run `git remote get-url origin` again.

   One-time recovery for the first PR from this control checkout (only after the error above): if `origin` points to the correct private control repository and `gh pr create` still reports the same error, run this once, then rerun the `gh pr create` command in step 4:

   ~~~bash
   gh repo set-default origin
   ~~~

   ~~~bash
   # After setting the default remote, run or retry this command.
   gh pr create \
     --base main \
     --head "$(git branch --show-current)" \
     --title "chore: update PreviewMesh from upstream" \
     --body "Review the upstream PreviewMesh changes before merging."
   ~~~

   Do not repeat this recovery command for later updates. Use `--repo` only for troubleshooting or an intentional cross-repository operation; it is not part of the normal flow.

   `gh pr create` creates the pull request only; it does not merge it. Keep the current generated `update/preview...` review branch checked out while the PR review and checks are pending. Do not switch to `main` or merge locally.

   After the PR is merged on GitHub, return to the private control checkout and synchronize the local `main` branch:

   ~~~bash
   git switch main
   git pull --ff-only origin main
   ~~~

   Removing the merged review branch locally or remotely is optional.

The updater preserves the private repository's application registrations in `config/repositories.json`; GitHub secrets are outside Git and are not changed. It does not update already-published application notification workflows automatically. Rerun `install` after merging an update when local managed files need refreshing.

Alternative direct-sync path:

Use this one-command path instead of the review-branch path when the operator intends to update and push `main` directly. Do not run it after the review-branch path or combine both paths for the same update. It requires `main` and a clean working tree, fast-forwards `main` from `origin`, fetches `upstream/main`, preserves `config/repositories.json`, runs the control-repository checks, commits the update, and pushes `main` only after validation succeeds. It does not create a pull request.

If the helper is already present in the private checkout, run:

~~~bash
# Run from the private deployment-management checkout.
scripts/sync-previewmesh.sh
~~~

If the helper is not yet present in the private checkout, invoke the public copy directly against that checkout. This command is the direct-sync path itself; it is not a separate preparation step:

~~~bash
# Run from the public PreviewMesh checkout.
scripts/sync-previewmesh.sh --repo-dir /path/to/previewmesh-control
~~~
