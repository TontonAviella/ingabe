export default function PrivacyPolicy() {
  return (
    <div className="max-w-3xl mx-auto p-8 prose dark:prose-invert">
      <h1>Privacy Policy</h1>
      <p className="text-sm text-muted-foreground">Last updated: February 2026</p>

      <h2>1. Who We Are</h2>
      <p>
        Ingabe Ltd. ("Ingabe", "we", "our") operates mundi.ai, an open-source, AI-native web GIS platform. This policy describes how we
        collect, use, and protect your data.
      </p>

      <h2>2. Data We Collect</h2>
      <h3>Account Information</h3>
      <p>
        When you sign in via our authentication provider (Clerk), we receive your email address and a unique user identifier. We do not
        store passwords.
      </p>
      <h3>Geospatial Data</h3>
      <p>
        You may upload, create, or connect to geospatial datasets (vector, raster, point cloud). This data is stored in our PostgreSQL
        database and S3-compatible object storage.
      </p>
      <h3>Chat Messages</h3>
      <p>
        Conversations with the AI assistant (Sage) are stored to maintain chat history within your projects. Messages are sent to OpenAI for
        processing and are subject to{' '}
        <a href="https://openai.com/policies/privacy-policy" target="_blank" rel="noopener noreferrer">
          OpenAI's privacy policy
        </a>
        .
      </p>
      <h3>Usage Analytics</h3>
      <p>We use privacy-friendly analytics to understand how the platform is used. We do not sell your data to third parties.</p>

      <h2>3. How We Use Your Data</h2>
      <ul>
        <li>To provide and improve the GIS platform</li>
        <li>To process AI assistant requests</li>
        <li>To maintain your projects and map versions</li>
        <li>To diagnose technical issues</li>
      </ul>

      <h2>4. Data Sharing</h2>
      <p>We share data with:</p>
      <ul>
        <li>
          <strong>OpenAI</strong> — chat messages for AI processing
        </li>
        <li>
          <strong>Clerk</strong> — authentication provider
        </li>
        <li>
          <strong>Cloud infrastructure</strong> — hosting providers for storage and compute
        </li>
      </ul>
      <p>We do not sell personal data.</p>

      <h2>5. Data Retention</h2>
      <p>
        Your data is retained as long as your account is active. Deleted projects are soft-deleted and permanently removed after 30 days.
        You may request full data deletion by contacting us.
      </p>

      <h2>6. Your Rights</h2>
      <p>You have the right to:</p>
      <ul>
        <li>Access your personal data</li>
        <li>Export your geospatial data</li>
        <li>Request deletion of your account and data</li>
        <li>Withdraw consent for analytics</li>
      </ul>

      <h2>7. Security</h2>
      <p>
        We use TLS encryption, JWT-based authentication, and role-based access controls. Geospatial data is stored in encrypted-at-rest
        storage.
      </p>

      <h2>8. Open Source</h2>
      <p>
        Ingabe is open source under AGPLv3. Self-hosted instances are responsible for their own data handling practices. This policy applies
        only to Ingabe-operated services.
      </p>

      <h2>9. Contact</h2>
      <p>
        For privacy questions, contact us at <a href="mailto:privacy@ingabe.com">privacy@ingabe.com</a>.
      </p>
    </div>
  );
}
