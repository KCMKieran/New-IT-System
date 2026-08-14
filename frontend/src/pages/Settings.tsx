import BlurText from "@/components/blur-text"

// Placeholder while the real settings page is being built. The View Profiles
// UI that used to live here moved to /cfg/view-profiles (see ViewProfiles.tsx).
export default function SettingsPage() {
  return (
    <div className="flex min-h-svh items-center justify-center">
      <BlurText
        text="Settings 开发ing"
        delay={150}
        animateBy="words"
        direction="top"
        className="text-3xl font-semibold"
        repeatEveryMs={5000}
      />
    </div>
  )
}
