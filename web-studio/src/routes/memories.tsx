import { createFileRoute } from '@tanstack/react-router'
import { DirectoryPage } from './directory/-directory-page'

export const Route = createFileRoute('/memories')({
  component: () => <DirectoryPage memories />,
})
