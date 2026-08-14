/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
        display: ['"Instrument Serif"', 'serif'],
        sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
      },
      colors: {
        ink: {
          950: "#0a0b0d",
          900: "#0f1115",
          800: "#15181f",
          700: "#1f242e",
          600: "#2a3140",
        },
        amber: {
          glow: "#ffb547",
        },
        signal: {
          ok:   "#5ce1a3",
          warn: "#ffb547",
          crit: "#ff5470",
        },
      },
      boxShadow: {
        "glow-warn": "0 0 24px rgba(255,181,71,0.35)",
        "glow-crit": "0 0 24px rgba(255,84,112,0.35)",
        "glow-ok":   "0 0 24px rgba(92,225,163,0.30)",
      },
    },
  },
  plugins: [],
};
