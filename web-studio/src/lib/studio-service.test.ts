import { describe, expect, it } from 'vitest'
import {
  classifyCapabilityError,
  classifyCapabilityResponse,
  featureForPath,
  identifyServiceProvider,
  isStudioFeatureVisible,
} from './studio-service'
import { resolveStudioManagementCapabilities } from './studio-permissions'

describe('service and permission boundaries', () => {
  it('recognizes the official hosted URL without discarding its path', () => {
    expect(
      identifyServiceProvider(
        'https://api.vikingdb.cn-beijing.volces.com/openviking',
      ),
    ).toBe('volcengine')
    expect(
      identifyServiceProvider(
        'https://api.vikingdb.cn-beijing.volces.com.evil.example/openviking',
      ),
    ).toBe('unknown')
    expect(
      identifyServiceProvider('https://custom.company.example/openviking'),
    ).toBe('unknown')
    expect(identifyServiceProvider('http://localhost:1933')).toBe('opensource')
  })
  it.each(['volcengine', 'unknown'] as const)(
    'never maps %s admin/root data identity into native management',
    (serviceProvider) => {
      for (const role of ['admin', 'root'] as const) {
        expect(
          resolveStudioManagementCapabilities({
            serviceProvider,
            hasControlCredential: true,
            isRoleLoading: false,
            role,
            serverMode: 'api_key',
          }),
        ).toEqual({ canManageAccounts: false, canManageUsers: false })
      }
    },
  )
  it('distinguishes gateway blocking, user permission and uncertain failure', () => {
    expect(
      classifyCapabilityError({
        statusCode: 403,
        responseBody: { ResponseMetadata: { Error: { Code: 'ApiBlocked' } } },
      }),
    ).toBe('unavailable')
    expect(
      classifyCapabilityError({ statusCode: 403, code: 'PERMISSION_DENIED' }),
    ).toBe('forbidden')
    expect(classifyCapabilityError({ statusCode: 401 })).toBe('unknown')
    expect(classifyCapabilityError({ statusCode: 500 })).toBe('unknown')
    expect(classifyCapabilityError({})).toBe('unknown')
  })
  it('guards nested deep links with the same domain as navigation', () => {
    expect(featureForPath('/users/memory-templates')?.domain).toBe('management')
    expect(featureForPath('/playground')?.domain).toBe('extensions')
    expect(featureForPath('/directory')?.domain).toBe('workspace')
  })
})

describe('hosted feature visibility', () => {
  const monitoring = featureForPath('/monitoring')!
  it.each([undefined, 'unknown', 'unavailable', 'forbidden'] as const)(
    'hides an extension while its capability is %s',
    (state) => {
      expect(isStudioFeatureVisible('volcengine', monitoring, state)).toBe(
        false,
      )
    },
  )
  it('shows only confirmed extensions, and keeps public workspace navigation', () => {
    expect(isStudioFeatureVisible('volcengine', monitoring, 'supported')).toBe(
      true,
    )
    expect(
      isStudioFeatureVisible(
        'volcengine',
        featureForPath('/playground')!,
        'supported',
      ),
    ).toBe(false)
    expect(
      isStudioFeatureVisible('volcengine', featureForPath('/directory')!),
    ).toBe(true)
    expect(
      isStudioFeatureVisible(
        'volcengine',
        featureForPath('/users')!,
        'supported',
      ),
    ).toBe(false)
    expect(isStudioFeatureVisible('unknown', monitoring)).toBe(false)
    expect(
      isStudioFeatureVisible('opensource', featureForPath('/playground')!),
    ).toBe(true)
  })
})

describe('capability response contracts', () => {
  it('does not enable a page for generic JSON or a successful HTTP error envelope', () => {
    expect(classifyCapabilityResponse('watches', { message: 'proxy' })).toBe(
      'unknown',
    )
    expect(
      classifyCapabilityResponse('watches', {
        status: 'error',
        error: { code: 'ApiBlocked' },
      }),
    ).toBe('unavailable')
    expect(
      classifyCapabilityResponse('watches', {
        ResponseMetadata: { Error: { Code: 'ApiBlocked' } },
      }),
    ).toBe('unavailable')
    expect(classifyCapabilityResponse('watches', { result: [] })).toBe(
      'unknown',
    )
    expect(
      classifyCapabilityResponse('requestLogs', { result: { enabled: false } }),
    ).toBe('unavailable')
  })
  it('checks the read contract each supported hosted page consumes', () => {
    expect(
      classifyCapabilityResponse('watches', {
        status: 'ok',
        result: { tasks: [] },
      }),
    ).toBe('supported')
    expect(
      classifyCapabilityResponse('requestLogs', {
        result: { items: [], total: 0 },
      }),
    ).toBe('supported')
    expect(
      classifyCapabilityResponse('monitoring', {
        result: { components: {}, is_healthy: true },
      }),
    ).toBe('supported')
    expect(
      classifyCapabilityResponse('monitoring', { result: { status: 'ok' } }),
    ).toBe('unknown')
  })
  it.each(['/home', '/vikingbot', '/compile'])(
    'keeps %s hidden until the entire hosted page is adapted',
    (path) => {
      expect(
        isStudioFeatureVisible(
          'volcengine',
          featureForPath(path)!,
          'supported',
        ),
      ).toBe(false)
      expect(isStudioFeatureVisible('opensource', featureForPath(path)!)).toBe(
        true,
      )
    },
  )
})
