import { SignUp } from "@clerk/nextjs";

export default function SignUpPage() {
  return (
    <div className="min-h-screen bg-[#f7f5f2] flex items-center justify-center">
      <SignUp />
    </div>
  );
}
