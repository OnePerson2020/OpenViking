import { describe, expect, it } from 'vitest'
import { resolveDomainDestination } from './studio-navigation'

describe('domain navigation', () => {
  it('returns to the last visible workspace page', () => {
    expect(
      resolveDomainDestination('workspace', '/retrieval', [
        '/directory',
        '/retrieval',
      ]),
    ).toBe('/retrieval')
  })
  it('retains detail routes under a visible feature', () => {
    expect(
      resolveDomainDestination('management', '/users/groups', ['/users']),
    ).toBe('/users/groups')
  })
  it('falls back when the remembered capability is no longer available', () => {
    expect(
      resolveDomainDestination('extensions', '/monitoring', [
        '/directory',
        '/watches',
      ]),
    ).toBe('/watches')
  })
  it('rejects a page from another domain', () => {
    expect(
      resolveDomainDestination('workspace', '/users', ['/directory', '/users']),
    ).toBe('/directory')
  })
})
