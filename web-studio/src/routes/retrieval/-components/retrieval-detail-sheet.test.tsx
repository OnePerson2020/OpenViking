// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import type { TFunction } from 'i18next'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RetrievalDetailSheet } from './retrieval-detail-sheet'

vi.mock('#/hooks/use-studio-service', () => ({
  useStudioService: () => ({ provider: 'volcengine' }),
}))

const t = ((key: string) => key) as TFunction<'retrieval'>

afterEach(() => {
  cleanup()
})

describe('RetrievalDetailSheet', () => {
  it('keeps hosted result details readable without offering the unsupported playground', () => {
    render(
      <RetrievalDetailSheet
        detail={{
          abstract: 'Hosted result summary',
          contextType: 'resource',
          score: 0.8,
          uri: 'viking://resources/test.md',
          item: {
            uri: 'viking://resources/test.md',
            level: 2,
            score: 0.8,
            context_type: 'resource',
            abstract: 'Hosted result summary',
            category: '',
            match_reason: '',
          },
        }}
        onClose={vi.fn()}
        t={t}
      />,
    )
    expect(screen.getByText('Hosted result summary')).toBeDefined()
    expect(screen.queryByText('detail.openPlayground')).toBeNull()
  })
  it('shows only the result summary without loading full content', () => {
    render(
      <RetrievalDetailSheet
        detail={{
          abstract: 'Result summary',
          contextType: 'memory',
          score: 0.8,
          uri: 'viking://user/default/memories/test.md',
        }}
        onClose={vi.fn()}
        t={t}
      />,
    )

    expect(screen.getByText('detail.summary')).toBeDefined()
    expect(screen.getByText('Result summary')).toBeDefined()
    expect(screen.queryByText('detail.content')).toBeNull()
  })
})
