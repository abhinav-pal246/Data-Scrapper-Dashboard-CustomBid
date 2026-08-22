import { useState } from "react";
import reactLogo from "./assets/react.svg";
import viteLogo from "/vite.svg";
import "./styles.css";
import Footer from "./components/Footer.jsx"; // ✅ Correct import

import { FaInstagram, FaLinkedin, FaWeixin, FaBars } from "react-icons/fa";
function App() {
  const [count, setCount] = useState(0);

  return (
    <div className="App">
      
      
      {/* ✅ Footer should be outside of <h1> */}
      <Footer />  
    </div>
  );
}

export default App;
