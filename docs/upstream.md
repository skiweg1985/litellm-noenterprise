# Import and build the upstream baseline

## Reviewed source

The baseline is LiteLLM 1.106.6, revision
`61c6ab78393ef45683dd7d963d2a315bd845e20b`.
[Source release](https://github.com/BerriAI/litellm/releases/tag/v1.106.6).

`upstream/policy.json` pins the archive digest, included paths, exclusions, and
reviewed license notices. `upstream/manifest.json` records each original file's
SHA-256 and executable bit. `upstream/litellm` contains selected original bytes
on the `vendor/litellm` branch. Integration branches may apply narrow changes
there; those changes are separate commits, not new vendor provenance.

The root source license assigns content outside `enterprise/` to MIT. We also
reviewed nested notices and explicit source headers. The import excludes:

- `enterprise/` and its `litellm/proxy/enterprise` symlink;
- the prebuilt dashboard, which is rebuilt from selected source;
- old proxy-extras distribution archives and incidental test caches/archives;
- the Apache-2.0 NVIDIA capability classifier, to keep this source selection MIT;
- unrelated source areas, including the separately Apache-licensed Terraform provider.

Required MIT copyright and permission notices are retained. MIT-only describes
the imported LiteLLM source selection. Normal third-party runtime dependencies
retain their own open-source licenses; they are not all MIT.

A filename/notice scan is a regression check, not a substitute for reviewing a
new release. The importer rejects a changed archive, changed notices, unfamiliar
license files, non-MIT SPDX headers, and unreviewed links before writing files.

## Reproduce the import

Use Python 3.12 or newer. Download the pinned archive outside the repository:

```sh
curl -fL https://codeload.github.com/BerriAI/litellm/tar.gz/61c6ab78393ef45683dd7d963d2a315bd845e20b -o /tmp/litellm-source.tar.gz
python3 scripts/import_upstream.py --archive /tmp/litellm-source.tar.gz --output /tmp/litellm-import --manifest /tmp/litellm-manifest.json
cmp upstream/manifest.json /tmp/litellm-manifest.json
python3 scripts/audit_source.py
```

The staging output must be absent or empty. Do not unpack an unfiltered archive
into this repository or fetch upstream Git history into it.

A normal full clone can run the audit directly: it uses local `vendor/litellm`
when present, otherwise `origin/vendor/litellm`. For a checkout that fetched only
one branch, fetch this project's reviewed vendor branch explicitly:

```sh
git fetch origin refs/heads/vendor/litellm:refs/remotes/origin/vendor/litellm
python3 scripts/audit_source.py
```

The history audit rejects shallow checkouts. Use `git fetch --unshallow origin`
first, or configure CI checkout with full history (`fetch-depth: 0`). Missing
snapshots or history refs fail with recovery guidance. Use `--vendor-ref` to
select a particular reviewed snapshot and `--history-ref` to select the history
being audited; these options do not relax the full-history requirement.

## Build and check the image

```sh
docker build -t litellm-noenterprise:1.106.6 .
python3 -m unittest discover -s tests/baseline -v
python3 scripts/audit_image.py litellm-noenterprise:1.106.6
python3 scripts/smoke_image.py litellm-noenterprise:1.106.6
```

The [baseline PR workflow](../.github/workflows/baseline.yml) runs these checks
on a native Linux AMD64 runner with complete project history. It builds locally
on the runner and does not publish an image or deploy anything. Its job result
is the AMD64 validation evidence; the ARM64 results below do not establish AMD64
compatibility by themselves. No provider credentials or repository secrets are
needed.

The image uses digest-pinned Python, Node, and uv images, a hash-locked Python
dependency set, and the upstream npm lock. The dashboard is built from source
with Node 24 and npm 11, satisfying its declared engine requirements. Its
`.npmrc` is present during `npm ci`, enforcing engine checks and disabling
dependency lifecycle scripts. No package rebuild exceptions are needed.
LiteLLM and proxy-extras are built locally with dependency installation disabled,
so upstream's proxy extra cannot reinstall `litellm-enterprise`. The image uses
our pure-Python packaging metadata, leaving the optional native Rust extension
out. The Prisma client, CLI, and engines are generated at build time. Runtime
uses an unprivileged user and the cached Prisma CLI.

The project's MIT license is retained at
`/usr/share/licenses/litellm-noenterprise/LICENSE`; the upstream LiteLLM license
remains separately at `/usr/share/licenses/litellm/LICENSE`. The image audit
compares both documents byte for byte with their repository sources.

The image retains Node's complete license and third-party notices plus a
conservative collection from installed dashboard npm packages. These documents
are available under `/usr/share/licenses/litellm-noenterprise` and at
`/ui/third-party-notices/manifest.json` with source-relative document links.
The manifest records package identities, declared licenses, authors, and document
checksums. Collection includes build-only packages; it does not imply their code
is shipped. Only notice documents and metadata are copied, not dashboard
`node_modules` or npm build tooling. Python distribution and Prisma notices are
retained with their installed packages.

Pinned supplements in `runtime/licenses` retain source-release license files
omitted from the published Heroicons and Next.js ESLint packages. Additional
upstream MIT documents for `format` and `toggle-selection` are marked as later
supplements, alongside the original package declarations and README attribution.
The standard CC0-1.0 text accompanies the original `highlightjs-vue` declaration;
its later upstream license differs and is not applied to this package. These
supplements are not presented as files from those package releases.
Packages with only a README or no notice are explicitly marked for review. The `client-only` marker
package publishes an empty default browser export and an MIT declaration but no
notice document; no license text is invented for it. The collection is evidence
for notice retention, not a certification of all dependency license obligations.
The image audit verifies document hashes, and the smoke check downloads a notice
through the running UI. Running all baseline command tests also requires Node.js.

`runtime/requirements.in` is generated from the reviewed upstream core, proxy,
extra-proxy and bedrock-realtime dependency lists, excluding the two locally
built packages.
After dependency changes, regenerate and review the lock:

```sh
python3 scripts/runtime_requirements.py
uv pip compile runtime/requirements.in --python-version 3.12 --universal --generate-hashes -o runtime/requirements.lock
```

This build supports the standard proxy baseline, not every upstream optional
extra. Capability classification is unavailable because its implementation is
excluded; selecting it returns a configuration error. Normal routing loads
without that module. Live provider compatibility is not established by the
controlled smoke service. Base-image OS package repositories can change, so the
build is not claimed to produce byte-identical OCI images.

The image audit checks every saved final-image layer and installed Python
packages for excluded LiteLLM enterprise paths. Source selection, local package
builds, and dependency locks prevent those paths entering intermediate build
stages. Do not replace the base with an upstream LiteLLM image.

## Verified baseline

Validation on 2026-11-01 used an ARM64 Docker host and the image built from this
branch. The image startup and controlled chat request passed. The rebuilt
personal connection page and OAuth routes were present, and anonymous personal
credential access was denied. The image audit inspected all final image
layers, confirmed no enterprise distribution was installed, and checked retained
UI/audio assets. No live identity-provider or authenticated personal MCP flow is
claimed by these checks.

A repeated import produced the same manifest. A disposable Git exercise with two
successive vendor snapshots preserved an independent change in the same file
while merging an upstream update; vendor ancestry remained reachable.

## Update to a selected release

1. Create an update branch from current `origin/main`; review the selected
   release's source and licensing outside the repository.
2. Update the policy and import into an empty staging directory. Review the
   inventory and exclusions. Keep import-tool changes separate from source bytes.
3. On `vendor/litellm`, replace the selected source tree, policy, and manifest
   with the new import, including removals. Commit only that reviewed snapshot.
4. Merge `vendor/litellm` into the update branch with a normal merge commit.
   Resolve conflicts in independent patches and update build dependencies.
   [Changes from upstream](customizations.md) lists every patch and the files
   it touches.
5. Run source, image, runtime, and feature checks. Push the vendor/update branches
   and open a PR targeting protected `main`.
6. Merge through the PR with a merge commit. Do not squash or rebase away vendor
   ancestry: later three-way imports depend on the common vendor parent.

Never rewrite `main` or a published vendor history. Preserve existing project
documentation when updating the source snapshot. Check the resulting runtime,
not only whether Git resolved the merge.
