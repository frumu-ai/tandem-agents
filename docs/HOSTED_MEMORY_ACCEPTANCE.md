# Hosted memory acceptance

The actual-engine test uses the existing governed records API with verified
central identities and the shared v2 policy feed. It never uses model-tool
local memory writes. Session/project partitions remain storage partitions,
not permission grants. Team and curated write tiers remain unsupported.

The fixture maps personal and business notes using the existing independent
subject and department predicates:

| Intended space | Write labels | Expected access |
| --- | --- | --- |
| Personal note across departments | `private: true`, `tenant_shared: true` | Verified owner only; a department change does not transfer ownership |
| Private department note | `private: true`, `owner_org_unit_id` | Verified owner who still belongs to that department |
| Department knowledge | `private: false`, `owner_org_unit_id` | Current members of that department |
| Tenant knowledge | `private: false`, `tenant_shared: true` | Current authorized users of the tenant |

`tenant_shared` broadens the department axis only. It cannot override a private
owner predicate or the verified tenant boundary. Omitting department labels on
a write still stamps the collector's active department; it does not declare an
unrestricted personal space. Hosted writes require knowledge-scope metadata.
The fixture supplies a tenant/project-bound memory resource and explicitly
provisions read grants for the two departments in the existing persisted
org-unit access registry before starting the runtime. These grants are
additional to the owner and department predicates; `hosted.use` alone cannot
grant knowledge access. Production grant provisioning and external source
connector grants remain separate work.

`packages/runtime-bundle/tests/memory_engine_integration.py` starts the pinned
enterprise engine as UID1000, writes and recalls notes as two synthetic users,
changes one user's department through the authenticated policy feed, rejects
old assertions and repeats recall after restart. It checks known-ID mutation,
foreign tenant/department spoofing and unsupported tiers. The same fixture runs
for a second organization using the same artifact and repeated actor names.

The first CI run rejected the initial write because the fixture omitted required
knowledge-scope metadata. CI must pass with the corrected setup before claiming
privacy evidence. This fixture uses the default local storage crypto mode and
does not claim encryption acceptance. This test does not implement the
customer-configuration schema, sanitized export, source-connector grants,
encrypted off-site backup or clean-host recovery. Those remain separate gates.
