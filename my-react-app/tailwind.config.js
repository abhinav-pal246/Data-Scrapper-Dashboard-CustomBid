/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        gem: {
          header:   "#0b2a37",  // dark navy-teal masthead (matches gem.gov.in)
          nav:      "#103340",  // secondary nav band
          blue:     "#1f52a3",  // GeM royal-blue primary action
          bluedark: "#16357e",  // deep blue (hover / emphasis)
          link:     "#1a5eb8",  // link blue
          green:    "#3f9142",  // start dates / success
          orange:   "#ef7a1f",  // GeM orange accent (active / warning)
          red:      "#d9382a",
          yellow:   "#aeb800",
          bg:       "#eef2f5",  // page background
          card:     "#ffffff",
          border:   "#dbe3ea",
          text:     "#1f2d3a",  // primary text
          muted:    "#64748b",  // secondary text
        },
      },
      fontFamily: {
        gem: ['"Noto Sans"', "system-ui", '"Segoe UI"', "Roboto", "Arial", "sans-serif"],
      },
    },
  },
  plugins: [],
}
