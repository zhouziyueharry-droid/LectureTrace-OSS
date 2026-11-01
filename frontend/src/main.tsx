import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import App from './App.tsx'
import 'katex/dist/katex.min.css'
import './styles.css'
import './app-shell.css'
import { initTheme } from './theme.ts'

initTheme()

// 使用 data router，设置页才能用 useBlocker 在离开时提醒未保存的修改；具体路由仍在 App 内定义
const router = createBrowserRouter([{ path: '*', element: <App /> }])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
)
