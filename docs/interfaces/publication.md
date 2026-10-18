# Distribution publisher and signatures

This is the design boundary for stage 10; executable repository publication adapters
are not implemented. It is separate from [publishing the Python utility](../releasing.md).

PublicationPlanner will accept verified ArtifactManifest/VerificationRecord and
RepositoryConfig, producing exact hashes, identities and destinations in a
PublicationPlan. Publisher will execute upload/index/verification stages and return
PublicationResult.

Publication must be explicit. Signing is a separate operation; repository credentials
must not enter build containers. Signed files are derived artifacts referencing the
verified original hash. Upload success alone cannot grant repository-verified:
qualification requires installing the exact published version in a clean environment.
The same package identity with different bytes must not be overwritten.

Partial publication must retain completed stages and verify them during resume.
Cancellation preserves history and references to already uploaded objects. Signing,
indexing and installation failures remain visible. Nexus/local APT/local RPM adapters
will use an explicit registry and consistent availability/immutable-identity semantics.
