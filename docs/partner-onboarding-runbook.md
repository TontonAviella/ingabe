# Partner Onboarding Runbook

Step-by-step guide for onboarding a new partner organization to mundi.ai.
After completing these steps, partner users can upload private documents
and ask Sage questions that search both public and their private data.

## Prerequisites

- SSH access to prod (178.104.18.44)
- Clerk Dashboard access (clerk.com)
- Partner's organization name, contact email, and Clerk user accounts

## Step 0: Clerk Organizations (one-time setup)

Enable Clerk Organizations if not already done:

1. Go to Clerk Dashboard > Organizations
2. Enable the Organizations feature
3. Clerk JWTs automatically include `org_id`, `org_role`, `org_slug` when
   a user has an active organization selected

This step is per-Clerk-instance, not per-partner.

## Step 1: Create Clerk Organization

1. Clerk Dashboard > Organizations > Create Organization
2. Set name (e.g. "Soras Insurance")
3. Note the Organization ID: `org_2xxxxxxxxxxxxx`
4. Invite the partner's users to the organization

## Step 2: Create Database Organization Row

SSH to prod and insert the mapping row. This connects the Clerk org to
an internal UUID that RLS policies use for data isolation.

```bash
ssh root@178.104.18.44

docker exec mundiai-postgresdb-1 psql -U mundiuser -d mundidb -c "
  INSERT INTO organizations (name, slug, clerk_org_id, tier)
  VALUES (
    'Soras Insurance',
    'soras-insurance',
    'org_2xxxxxxxxxxxxx',   -- from Step 1
    'partner'
  )
  RETURNING id, name, slug, clerk_org_id;
"
```

Save the returned `id` (UUID). This is the internal org identifier.

## Step 3: Verify Resolution

Confirm the Clerk-to-internal mapping resolves:

```bash
docker exec mundiai-postgresdb-1 psql -U mundiuser -d mundidb -c "
  SELECT id, name, clerk_org_id
  FROM organizations
  WHERE clerk_org_id = 'org_2xxxxxxxxxxxxx';
"
```

Should return exactly one row.

## Step 4: Partner User Sign-In

1. Partner user signs into https://gis.nozalabs.rw
2. They click the Organization Switcher in the sidebar
3. They select their organization
4. Clerk JWT now carries `org_id` on every request

## Step 5: Document Upload

Partner uploads documents via the sidebar upload UI or API:

```
POST /api/partner/documents
Authorization: Bearer <clerk-jwt>
Content-Type: multipart/form-data

file: <pdf/txt/md/csv>
```

Or submit a URL:

```
POST /api/partner/documents/url
Authorization: Bearer <clerk-jwt>

{"url": "https://example.com/report.pdf", "title": "Q1 Report"}
```

## Step 6: Verify Isolation

From prod, confirm the uploaded document has correct access tags:

```bash
docker exec mundiai-postgresdb-1 psql -U mundiuser -d mundidb -c "
  SELECT slug, title, access_scope, partner_id
  FROM brain_pages
  WHERE access_scope = 'partner_internal'
  ORDER BY created_at DESC
  LIMIT 5;
"
```

Verify `partner_id` matches the org UUID from Step 2.

## Step 7: Cross-Tenant Verification

Confirm a non-partner user cannot see the private document:

1. Sign in as a user who is NOT in the partner's Clerk Organization
2. Ask Sage a question that should match the partner's document content
3. Sage should return only public data, not the partner's private document

From SQL, verify with a different partner_id GUC:

```bash
docker exec mundiai-postgresdb-1 psql -U mundiuser -d mundidb -c "
  SET app.partner_id = '00000000-0000-0000-0000-000000000000';
  SELECT slug, title FROM brain_pages
  WHERE access_scope = 'partner_internal'
    AND partner_id = '<partner-uuid-from-step-2>';
"
```

Should return 0 rows (RLS blocks the read).

## Step 8: Confirm Sage Integration

1. Sign in as the partner user with org selected
2. Ask Sage: "What cooperatives are insured in Gabiro?" (or a question
   relevant to the uploaded document)
3. Sage should return results from both public brain pages and the
   partner's private documents

## Troubleshooting

### Partner sees no private documents in Sage

1. Check Clerk JWT has `org_id`:
   - Browser DevTools > Application > Cookies/Storage, decode the JWT
   - Look for `org_id` claim
2. Check organizations table has the Clerk org mapping:
   ```sql
   SELECT * FROM organizations WHERE clerk_org_id = '<org_id_from_jwt>';
   ```
3. Check brain_pages have correct tags:
   ```sql
   SELECT slug, access_scope, partner_id FROM brain_pages
   WHERE partner_id = '<internal-org-uuid>';
   ```
4. Check container logs for resolution:
   ```bash
   docker logs mundi-app 2>&1 | grep "Resolved clerk org"
   ```

### Partner can see other partner's documents

This is a security incident. Immediately:

1. Check `app.partner_id` GUC is set:
   ```sql
   SELECT current_setting('app.partner_id', true);
   ```
2. Check RLS is active:
   ```sql
   SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'mundiuser';
   ```
   Both must be `f`. If not: `ALTER USER mundiuser NOSUPERUSER NOBYPASSRLS;`
3. Check the partner_isolation policy exists:
   ```sql
   SELECT * FROM pg_policies WHERE tablename = 'brain_pages'
     AND policyname LIKE '%partner%';
   ```

### Rollback (emergency)

To remove a partner's data and access:

```sql
-- Remove their brain pages
DELETE FROM brain_pages WHERE partner_id = '<org-uuid>';

-- Remove org membership
DELETE FROM user_organizations WHERE org_id = '<org-uuid>';

-- Remove org
DELETE FROM organizations WHERE id = '<org-uuid>';
```

Then delete the Clerk Organization from the Clerk Dashboard.
