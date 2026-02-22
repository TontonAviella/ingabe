export default function TermsOfService() {
  return (
    <div className="max-w-3xl mx-auto p-8 prose dark:prose-invert">
      <h1>Terms of Service</h1>
      <p className="text-sm text-muted-foreground">Last updated: February 2026</p>

      <h2>1. Acceptance</h2>
      <p>
        By accessing or using mundi.ai ("the Service"), operated by Ingabe Ltd. ("Ingabe", "we"), you agree to be bound by these Terms of
        Service. If you do not agree, do not use the Service.
      </p>

      <h2>2. Description of Service</h2>
      <p>
        Ingabe is an AI-native web GIS platform for geospatial data management, visualization, and analysis. The Service includes map
        creation, layer management, AI-assisted geoprocessing, and PostGIS database connections.
      </p>

      <h2>3. User Accounts</h2>
      <ul>
        <li>You must provide accurate account information.</li>
        <li>You are responsible for maintaining the security of your account.</li>
        <li>You must be at least 16 years old to use the Service.</li>
        <li>One person or entity per account.</li>
      </ul>

      <h2>4. Your Data</h2>
      <p>
        You retain ownership of all geospatial data, layers, and content you upload or create. You grant Ingabe a limited license to store,
        process, and display your data solely to provide the Service.
      </p>

      <h2>5. Acceptable Use</h2>
      <p>You agree not to:</p>
      <ul>
        <li>Upload malicious files or attempt to exploit the platform</li>
        <li>Use the AI assistant to generate harmful or illegal content</li>
        <li>Attempt to access other users' data without authorization</li>
        <li>Exceed reasonable usage limits or abuse API endpoints</li>
        <li>Reverse-engineer proprietary components of the Service</li>
      </ul>

      <h2>6. AI Assistant</h2>
      <p>
        The AI assistant (Kue) uses third-party language models. AI-generated outputs may contain errors. You are responsible for verifying
        the accuracy of any AI-generated analysis, statistics, or geoprocessing results before relying on them for decisions.
      </p>

      <h2>7. PostGIS Connections</h2>
      <p>
        When you connect external PostgreSQL/PostGIS databases, you are responsible for ensuring you have the right to access that data.
        Ingabe stores connection credentials encrypted and does not share them with third parties.
      </p>

      <h2>8. Service Availability</h2>
      <p>
        We strive for high availability but do not guarantee uninterrupted service. We may perform maintenance with reasonable notice. We
        are not liable for data loss — please maintain your own backups of critical data.
      </p>

      <h2>9. Open Source</h2>
      <p>
        Ingabe's source code is available under the GNU Affero General Public License v3 (AGPLv3) at{' '}
        <a href="https://github.com/Ingabe/mundi.ai" target="_blank" rel="noopener noreferrer">
          github.com/Ingabe/mundi.ai
        </a>
        . These Terms apply to the hosted service, not self-hosted instances.
      </p>

      <h2>10. Limitation of Liability</h2>
      <p>
        To the maximum extent permitted by law, Ingabe shall not be liable for any indirect, incidental, or consequential damages arising
        from your use of the Service, including but not limited to loss of data, revenue, or profits.
      </p>

      <h2>11. Termination</h2>
      <p>
        We may suspend or terminate your account for violation of these Terms. You may delete your account at any time. Upon termination,
        your data will be deleted per our Privacy Policy.
      </p>

      <h2>12. Changes to Terms</h2>
      <p>
        We may update these Terms from time to time. Continued use of the Service after changes constitutes acceptance of the updated Terms.
      </p>

      <h2>13. Contact</h2>
      <p>
        For questions about these Terms, contact us at <a href="mailto:legal@ingabe.com">legal@ingabe.com</a>.
      </p>
    </div>
  );
}
