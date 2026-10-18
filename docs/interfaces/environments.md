# Environment preparation

`config.environment_requirements(effective_data) -> dict` combines platform,
option, component and locale dependencies per target, separately for build/test.
Its hash enters EnvironmentManifest. Output paths and branding-only changes do not
force package reinstallation.

`planning.environments.prepare_environments(config, context)` runs only in a durable
worker. It checks local Linux Docker, fixes the base image ID, creates a labeled
container, verifies OS/architecture, installs APT dependencies including NLS gettext,
prepares locales and the build user/group, collects package/toolchain inventory and
checks catalog tool minimums. LLVM/Clang checks apply when LLVM is enabled. The
container is stopped and committed into a prepared image. Host compilers and package
managers are not invoked.

Recipes declare separate build/test inputs; the test image does not inherit the
build image. The implemented adapter is APT/DEB on Debian 13 / x86_64. Base images
can use registry references or local IDs. Online preparation pulls missing bases;
offline preparation only checks already provisioned images without networking.
OCI image archive/rootfs import is not implemented.

The manifest distinguishes local image ID, base image ID, base registry digests,
registry manifest digest and image archive SHA256. The last two are null for a local
commit. A RepoDigests entry is not claimed to be a platform-specific manifest.
APT repositories are mutable: installed versions and resulting image IDs are
recorded, without promising byte-identical future APT installations.

The build image holds a local APT runtime repository. Runtime dependencies and their
transitive closure are downloaded using an empty dpkg status database. The manifest
records filenames, hashes, sizes and repository hash. The test image does not
implicitly receive product runtime libraries; validation installs them from this
repository without networking. Preparing an image does not qualify PostgreSQL.

Resource intent/name/labels are recorded before create. Cancellation stops and
removes the owned container; orphan recovery uses exact references. Prepared images
are outputs, not transient containers, and are retained. Failure/cancellation does
not publish a new EnvironmentManifest. Each image commit updates
`prepared-images.json`. Resume verifies requirements and immutable image availability,
reuses confirmed images and repeats unfinished preparation. Images are not pruned
automatically.
