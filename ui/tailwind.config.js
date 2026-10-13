/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Палитра проекта (см. :root в styles/glass.css)
        'fh-bg-1': '#0a0e27',
        'fh-bg-2': '#1a1f3a',
        'fh-profit': '#00ffa3',
        'fh-loss': '#ff4d6d',
        'fh-primary': '#7c5cff',
        'fh-warn': '#ffb547',
        'fh-info': '#4dc9ff',
        surface: 'rgba(255,255,255,0.06)',
      },
      fontFamily: {
        sans: ['Inter', 'Inter Variable', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      backdropBlur: { glass: '24px' },
      borderRadius: { glass: '24px', pill: '999px' },
      transitionTimingFunction: { md3: 'cubic-bezier(0.4, 0, 0.2, 1)' },
      keyframes: {
        'pulse-ring': {
          '0%': { transform: 'scale(0.95)', opacity: '0.7' },
          '70%': { transform: 'scale(1.15)', opacity: '0' },
          '100%': { transform: 'scale(1.15)', opacity: '0' },
        },
        'slide-up': { from: { transform: 'translateY(12px)', opacity: '0' }, to: { transform: 'none', opacity: '1' } },
      },
      animation: {
        'pulse-ring': 'pulse-ring 2s cubic-bezier(0.4,0,0.2,1) infinite',
        'slide-up': 'slide-up 260ms cubic-bezier(0.4,0,0.2,1) both',
      },
    },
  },
  plugins: [],
};
