// src/pages/Login.tsx
import { useNavigate } from "react-router-dom"
import { useAuth } from "@/providers/auth-provider"
import { LoginForm } from "@/components/login-form"

export default function LoginPage() {
  const navigate = useNavigate()
  const { login } = useAuth()

  // Handle form submit from <LoginForm />
  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const data = new FormData(e.currentTarget)
    const email = String(data.get("email") || "")
    const password = String(data.get("password") || "")
    await login(email, password)
    navigate("/", { replace: true })
  }

  // Add a hero image on the left; place image under /public/images/login-hero.png
  return (
    <div className="grid min-h-svh grid-cols-1 md:grid-cols-2">
      <div className="hidden md:block bg-muted">
        <img
          src="/images/login-hero.png"
          alt="Welcome"
          className="h-full w-full object-cover"
        />
      </div>
      <div className="flex items-center justify-center p-6">
        <LoginForm onSubmit={handleSubmit} />
      </div>
    </div>
  )
}