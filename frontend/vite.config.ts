import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const backend = process.env.POWERCOPY_API_URL || 'http://127.0.0.1:8000'
const proxy = {
  '/api/workbench': { target: backend },
  '/health': { target: backend },
}

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
})
