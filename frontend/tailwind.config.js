/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#08090c',
          900: '#0d0f14',
          850: '#12151c',
          800: '#171b24',
          700: '#232936',
          600: '#333b4d',
          500: '#4b5568',
        },
        paper: {
          DEFAULT: '#eef1f6',
          dim: '#a7b0c0',
          faint: '#6b7488',
        },
        paypal: { DEFAULT: '#0070ba', bright: '#2b9fe0', deep: '#003087' },
        held: { DEFAULT: '#e8b53c', dim: '#3a2f14' },
        released: { DEFAULT: '#3ecf8e', dim: '#12352a' },
        voided: { DEFAULT: '#e8734a', dim: '#3a1f16' },
      },
      fontFamily: {
        sans: ['Inter var', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'JetBrains Mono', 'Menlo', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
    },
  },
  plugins: [],
}
