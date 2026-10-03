// @vitest-environment jsdom
import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useStudioService } from './use-studio-service'

const state = vi.hoisted(() => ({
  selection: 'auto',
  url: 'https://api.vikingdb.cn-beijing.volces.com/openviking',
  mode: 'api_key',
  health: null as Record<string, unknown> | null,
}))
vi.mock('./use-app-connection', () => ({
  useAppConnection: () => ({
    connection: { baseUrl: state.url, serviceSelection: state.selection },
    serverMode: state.mode,
    serverHealth: state.health,
  }),
}))
afterEach(() => {
  cleanup()
  state.selection = 'auto'
  state.mode = 'api_key'
  state.health = null
})
it('identifies a hosted deployment from the connection owner health result', () => {
  const view = renderHook(useStudioService)
  expect(view.result.current.ready).toBe(false)
  state.health = { auth_mode: 'api_key', version: 'v0.4.20.6' }
  view.rerender()
  expect(view.result.current.provider).toBe('volcengine')
  expect(view.result.current.version).toBe('v0.4.20.6')
})
it('never classifies an offline connection or non-health JSON as ready', () => {
  state.selection = 'volcengine'
  state.mode = 'offline'
  state.health = { version: 'stale' }
  const view = renderHook(useStudioService)
  expect(view.result.current.ready).toBe(false)
  expect(view.result.current.provider).toBe('unknown')
  state.mode = 'api_key'
  state.health = { message: 'proxy' }
  view.rerender()
  expect(view.result.current.ready).toBe(false)
})
it('does not let a manual override turn an official hosted deployment into native management', () => {
  state.selection = 'opensource'
  state.health = { auth_mode: 'api_key', version: 'v0.4.20.6' }
  expect(renderHook(useStudioService).result.current.provider).toBe(
    'volcengine',
  )
})
