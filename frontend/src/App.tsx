import { Link, Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell.tsx'
import { LibraryProvider } from './library.tsx'
import HomePage from './pages/HomePage.tsx'
import CoursePage from './pages/CoursePage.tsx'
import InboxPage from './pages/InboxPage.tsx'
import KeyframesPage from './pages/KeyframesPage.tsx'
import SettingsPage from './pages/SettingsPage.tsx'
import VideoPage from './pages/VideoPage.tsx'

export default function App() {
  return (
    <LibraryProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<HomePage />} />
          <Route path="/courses/:id" element={<CoursePage />} />
          <Route path="/inbox" element={<InboxPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="/videos/:id" element={<VideoPage />} />
        <Route path="/videos/:id/keyframes" element={<KeyframesPage />} />
        <Route
          path="*"
          element={
            <div className="page-center">
              <p>页面不存在。</p>
              <Link to="/">返回课程库</Link>
            </div>
          }
        />
      </Routes>
    </LibraryProvider>
  )
}
