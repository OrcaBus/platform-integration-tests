def load_reporter_template() -> str:
    """
    Load reporter template.
    """
    return """
<!DOCTYPE html>
        <html lang="en">
          <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Integration Test Report - {{ testId }}</title>
            <style>
              * { box-sizing: border-box; margin: 0; padding: 0; }
              body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
                line-height: 1.6;
                color: #333;
              }
              .container {
                max-width: 1400px;
                margin: 0 auto;
                background: white;
                border-radius: 12px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                overflow: hidden;
              }
              .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 40px;
                text-align: center;
              }
              .header h1 {
                font-size: 2.5em;
                margin-bottom: 10px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 15px;
              }
              .header .icon {
                font-size: 1.2em;
              }
              .header-info {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-top: 30px;
                padding-top: 30px;
                border-top: 1px solid rgba(255,255,255,0.2);
              }
              .header-info-item {
                text-align: left;
              }
              .header-info-item strong {
                display: block;
                opacity: 0.9;
                font-size: 0.9em;
                margin-bottom: 5px;
              }
              .header-info-item span {
                font-size: 1.1em;
                word-break: break-word;
                overflow-wrap: break-word;
              }
              .content {
                padding: 40px;
              }
              .status-badge {
                display: inline-block;
                padding: 8px 20px;
                border-radius: 20px;
                font-weight: bold;
                font-size: 0.9em;
                text-transform: uppercase;
                letter-spacing: 1px;
              }
              .status-passed {
                background: #10b981;
                color: white;
              }
              .status-failed {
                background: #ef4444;
                color: white;
              }
              .status-timeout {
                background: #f59e0b;
                color: white;
              }
              .status-running {
                background: #3b82f6;
                color: white;
              }
              .summary-section {
                background: #f8fafc;
                border-radius: 8px;
                padding: 30px;
                margin: 30px 0;
                border-left: 4px solid #667eea;
              }
              .summary-section h2 {
                font-size: 1.8em;
                margin-bottom: 20px;
                color: #1e293b;
                display: flex;
                align-items: center;
                gap: 10px;
              }
              .summary-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-top: 20px;
              }
              .summary-card {
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                text-align: center;
                transition: transform 0.2s;
              }
              .summary-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
              }
              .summary-card .value {
                font-size: 2.5em;
                font-weight: bold;
                color: #667eea;
                margin: 10px 0;
              }
              .summary-card .label {
                color: #64748b;
                font-size: 0.9em;
                text-transform: uppercase;
                letter-spacing: 1px;
              }
              .summary-card.matched .value { color: #10b981; }
              .summary-card.missing .value { color: #ef4444; }
              .summary-card.unexpected .value { color: #f59e0b; }
              .events-section {
                margin: 40px 0;
                padding: 30px;
                background: #f8fafc;
                border-radius: 8px;
              }
              .events-section h2 {
                font-size: 1.8em;
                margin-bottom: 25px;
                color: #1e293b;
                display: flex;
                align-items: center;
                gap: 10px;
                padding-bottom: 15px;
                border-bottom: 2px solid #e2e8f0;
              }
              .events-table {
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 8px;
                overflow: hidden;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                margin-top: 20px;
              }
              .events-table thead {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
              }
              .events-table th {
                padding: 15px;
                text-align: left;
                font-weight: 600;
                text-transform: uppercase;
                font-size: 0.85em;
                letter-spacing: 0.5px;
              }
              .events-table tbody tr {
                border-bottom: 1px solid #e2e8f0;
                transition: background 0.2s;
              }
              .events-table tbody tr:hover {
                background: #f8fafc;
              }
              .events-table tbody tr:last-child {
                border-bottom: none;
              }
              .events-table td {
                padding: 15px;
                vertical-align: top;
              }
              .events-table code {
                background: #f1f5f9;
                padding: 4px 8px;
                border-radius: 4px;
                font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
                font-size: 0.9em;
                color: #475569;
              }
              .events-table code.event-id {
                background: #dbeafe;
                color: #1e40af;
                word-break: break-all;
              }
              .events-table .order-col {
                text-align: center;
                font-weight: bold;
                color: #667eea;
                width: 60px;
              }
              .events-table .timestamp {
                font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
                font-size: 0.85em;
                color: #64748b;
              }
              .events-table .expected-event {
                max-width: 500px;
              }
              .events-table pre {
                background: #1e293b;
                color: #e2e8f0;
                padding: 15px;
                border-radius: 6px;
                overflow-x: auto;
                font-size: 0.85em;
                line-height: 1.5;
                margin: 0;
              }
              .s3-link {
                text-align: center;
                width: 50px;
              }
              .s3-link a {
                display: inline-block;
                text-decoration: none;
                font-size: 1.2em;
                color: #667eea;
                transition: transform 0.2s, color 0.2s;
              }
              .s3-link a:hover {
                transform: scale(1.2);
                color: #764ba2;
              }
              .s3-link a:visited {
                color: #667eea;
              }
              .empty-state {
                text-align: center;
                padding: 40px;
                color: #64748b;
              }
              .empty-state .icon {
                font-size: 3em;
                display: block;
                margin-bottom: 10px;
              }
              .raw-result {
                background: #1e293b;
                color: #e2e8f0;
                padding: 25px;
                border-radius: 8px;
                margin-top: 30px;
              }
              .raw-result h2 {
                color: #e2e8f0;
                margin-bottom: 15px;
                font-size: 1.5em;
              }
              .raw-result pre {
                background: #0f172a;
                padding: 20px;
                border-radius: 6px;
                overflow-x: auto;
                font-size: 0.9em;
                line-height: 1.6;
              }
              @media (max-width: 768px) {
                .header h1 { font-size: 1.8em; }
                .content { padding: 20px; }
                .summary-grid { grid-template-columns: 1fr; }
                .events-table { font-size: 0.85em; }
                .events-table th,
                .events-table td { padding: 10px 8px; }
              }
            </style>
          </head>
          <body>
            <div class="container">
              <div class="header">
                <h1>
                  <span class="icon">📊</span>
                  Integration Test Report
                </h1>
                <div class="header-info">
                  <div class="header-info-item">
                    <strong>Test ID</strong>
                    <span>{{ testId }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Service</strong>
                    <span>{{ serviceName }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Status</strong>
                    <span class="status-badge status-{{ runStatus }}">{{ runStatus }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Started At</strong>
                    <span>{{ startedAt }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Verified At</strong>
                    <span>{{ verifiedAt }}</span>
                  </div>
                </div>
              </div>

              <div class="content">
                <div class="summary-section">
                  <h2><span>📈</span> Summary</h2>
                  <div class="summary-grid">
                    <div class="summary-card">
                      <div class="label">Total Expected</div>
                      <div class="value">{{ totalExpected }}</div>
                    </div>
                    <div class="summary-card matched">
                      <div class="label">✓ Matched</div>
                      <div class="value">{{ matchedCount }}</div>
                    </div>
                    <div class="summary-card missing">
                      <div class="label">✗ Missing</div>
                      <div class="value">{{ missingCount }}</div>
                    </div>
                    <div class="summary-card unexpected">
                      <div class="label">⚠ Unexpected</div>
                      <div class="value">{{ unexpectedCount }}</div>
                    </div>
                  </div>
                </div>

                <div class="events-section">
                  <h2><span>✅</span> Matched Events</h2>
                  {{ matchedEventsTable }}
                </div>

                <div class="events-section">
                  <h2><span>❌</span> Missing Events</h2>
                  {{ missingEventsTable }}
                </div>

                <div class="events-section">
                  <h2><span>⚠️</span> Unexpected Events</h2>
                  {{ unexpectedEventsTable }}
                </div>

                <div class="raw-result">
                  <h2>🔍 Verify Result (Raw JSON)</h2>
                  <pre>{{ verifyResultJson }}</pre>
                </div>
              </div>
            </div>
          </body>
        </html>
        """
