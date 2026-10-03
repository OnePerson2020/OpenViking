// @vitest-environment jsdom
import type { ComponentType } from 'react'
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

import { Route } from './route'

const { probe, reconnect, serviceState, config } = vi.hoisted(() => ({
  probe: vi.fn(),
  reconnect: vi.fn(),
  serviceState: { provider: 'opensource' },
  config: { url: 'http://localhost:1933', selection: 'auto' },
}))
vi.mock('#/lib/admin', () => ({ probeStudioConnection: probe }))
vi.mock('#/hooks/use-app-connection', () => ({
  useAppConnection: () => ({
    connection: {
      baseUrl: config.url,
      serviceSelection: config.selection,
      accountId: 'account-a',
      userId: 'alice',
      adminApiKey: '',
      apiKey: '',
    },
    serverMode: 'trusted',
    saveConnection: vi.fn(),
    reconnect,
  }),
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { resolvedLanguage: 'en' },
  }),
}))
vi.mock('#/hooks/use-studio-service', () => ({
  useStudioService: () => ({
    provider: serviceState.provider,
    version: '',
    isChecking: false,
    health: { refetch: vi.fn() },
  }),
}))
afterEach(cleanup)

it.each([false, undefined, true])(
  'bases the Root key guide on server metadata: %s',
  async (required) => {
    probe.mockResolvedValue({
      admin: {
        state: 'error',
        statusCode: 403,
        detail: 'Management unavailable',
      },
      data: { state: 'ok', detailCode: 'tenantDataAvailable' },
      rootApiKeyRequired: required,
    })
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const Settings = Route.options.component as ComponentType & {
      preload?: () => Promise<unknown>
    }
    await Settings.preload?.()
    render(
      <QueryClientProvider client={client}>
        <Settings />
      </QueryClientProvider>,
    )
    await waitFor(
      () => expect(screen.getByText('Management unavailable')).toBeTruthy(),
      { timeout: 5000 },
    )
    expect(
      Boolean(screen.queryByText('connection.keyGuide.trusted.title')),
    ).toBe(required === true)
    client.clear()
  },
)

it('keeps management credentials editable after authentication fails and retries the connection owner', async () => {
  serviceState.provider = 'unknown'
  config.url = 'https://custom.example'
  config.selection = 'opensource'
  probe.mockResolvedValue({
    admin: { state: 'skipped' },
    data: { state: 'skipped' },
  })
  const client = new QueryClient()
  const Settings = Route.options.component as ComponentType
  render(
    <QueryClientProvider client={client}>
      <Settings />
    </QueryClientProvider>,
  )
  expect(document.getElementById('settings-root-api-key')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'studio:retry' }))
  expect(reconnect).toHaveBeenCalledTimes(1)
  client.clear()
  serviceState.provider = 'opensource'
  config.url = 'http://localhost:1933'
  config.selection = 'auto'
})
