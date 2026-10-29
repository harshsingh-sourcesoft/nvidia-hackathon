import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        surface: '#1e1e2e',
        toolbar: '#13131f',
        border: '#2e2e4a',
        accent: '#4f8ef7',
        'accent-hover': '#6ba3f7',
        'text-primary': '#e2e8f0',
        'text-muted': '#8892a4',
      },
    },
  },
  plugins: [],
};
export default config;
