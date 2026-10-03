// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DirectoryPage } from './-directory-page'

vi.mock('@tanstack/react-router', () => ({
  Link: ({
    to,
    children,
    ...props
  }: {
    to: string
    children: React.ReactNode
  }) => (
    <a {...props} href={to}>
      {children}
    </a>
  ),
}))

const state = vi.hoisted(() => ({ paths: [] as string[] }))
vi.mock('#/hooks/use-app-connection', () => ({
  useAppConnection: () => ({ connection: { userId: 'alice' } }),
}))
vi.mock('#/hooks/use-studio-service', () => ({
  useStudioService: () => ({ provider: 'volcengine' }),
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}))
vi.mock('#/routes/resources/-hooks/viking-fm', () => ({
  useVikingFsList: (path: string) => {
    state.paths.push(path)
    return {
      data: {
        entries:
          path === 'viking://~/'
            ? [
                {
                  name: 'memories',
                  uri: 'viking://user/alice/memories/',
                  isDir: true,
                },
              ]
            : [],
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }
  },
}))
vi.mock('#/routes/resources/-components/lazy-file-preview', () => ({
  LazyFilePreview: () => <span data-testid="preview" />,
}))
afterEach(cleanup)
it('returns from a server-resolved URI to the personal alias without exposing the global root', () => {
  render(<DirectoryPage />)
  fireEvent.click(
    screen.getByRole('button', { name: 'memories', pressed: false }),
  )
  expect(state.paths.at(-1)).toBe('viking://user/alice/memories/')
  fireEvent.click(screen.getByRole('button', { name: 'back' }))
  expect(state.paths.at(-1)).toBe('viking://~/')
  expect(
    screen.getByRole('button', { name: 'back' }).hasAttribute('disabled'),
  ).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'shared' }))
  expect(state.paths.at(-1)).toBe('viking://resources/')
})
