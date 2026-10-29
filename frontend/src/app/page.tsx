export default function Home() {
  // v param forces browser to fetch the latest editor.html and bypass any cache
  return (
    <iframe
      src="/editor.html?v=261"
      style={{ width: '100%', height: '100vh', border: 'none', display: 'block' }}
      title="PlanLift Editor"
    />
  );
}
