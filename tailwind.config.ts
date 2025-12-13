import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        card: "#111827",
        accent: "#14b8a6"
      },
      boxShadow: {
        card: "0 10px 25px rgba(0, 0, 0, 0.35)"
      }
    }
  },
  plugins: []
};

export default config;
