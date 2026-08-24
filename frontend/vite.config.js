import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    // In local dev (npm run dev) proxy API calls to the FastAPI server on
    // :8000 so the frontend can just call relative paths like "/orders"
    // both in dev and once it's built and served by FastAPI itself.
    proxy: {
      '/orders': 'http://localhost:8000',
      '/inventory': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
