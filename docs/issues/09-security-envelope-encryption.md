# Security: KMS/Vault envelope encryption and key rotation

labels: security

Replace the single derived Fernet key with envelope encryption.

- `KeyProvider` interface: local (dev), AWS KMS, GCP KMS, Vault Transit
- per-workspace data keys, stored wrapped; `inkcast rotate-keys` re-wraps without downtime

**Done when:** rotation is tested and a leaked database alone yields no usable secret.
