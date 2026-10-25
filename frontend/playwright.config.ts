import { defineConfig } from '@playwright/test';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '..');
export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1, timeout: 45000,
  expect: { timeout: 8000 }, reporter: [['list'], ['json', { outputFile: '../work/playwright-results.json' }]],
  use: { baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:8081', headless: true, viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: process.env.E2E_BASE_URL ? undefined : {
    command: './scripts/run.sh', cwd: root, url: 'http://127.0.0.1:8081/api/health/ready', reuseExistingServer: false,
    env: { APP_PORT: '8081', DATABASE_URL: `sqlite:///${root}/work/e2e-${Date.now()}.db`, LOGIN_IP_LIMIT: '10000', LOGIN_ACCOUNT_LIMIT: '1000' },
    timeout: 30000,
  },
});
