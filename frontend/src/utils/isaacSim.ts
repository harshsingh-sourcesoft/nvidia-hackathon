// Isaac Sim launch/poll routes (isaac-sim/launch-and-open, isaac-sim/task/<id>)
// live in api_server_mrcnn.py on port 8001, the same server as /analyze — a
// now-unused standalone api_server.py once served an identical set of
// routes on 8003, which is where this constant used to point. Nothing has
// listened on 8003 since that code was consolidated into api_server_mrcnn.py,
// so every "Open in Isaac Sim" request landed on a dead port (or whatever
// happened to be squatting it), producing a non-JSON response in the browser.
const API_BASE = 'http://localhost:8001';

export type TaskStatus =
  | 'starting'
  | 'checking'
  | 'launching'
  | 'waiting'
  | 'sending'
  | 'done'
  | 'error';

export interface IsaacTask {
  status: TaskStatus;
  message: string;
}

/** Upload the GLB and start the background launch-and-open task. Returns task_id. */
export async function startIsaacTask(blob: Blob): Promise<string> {
  const form = new FormData();
  form.append('file', blob, 'planlift_model.glb');
  const res = await fetch(`${API_BASE}/isaac-sim/launch-and-open`, {
    method: 'POST',
    body: form,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
  return data.task_id as string;
}

/** Poll the task until it reaches 'done' or 'error'. Calls onUpdate on each poll. */
export async function pollIsaacTask(
  taskId: string,
  onUpdate: (task: IsaacTask) => void,
  intervalMs = 2000,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const res = await fetch(`${API_BASE}/isaac-sim/task/${taskId}`);
        const task: IsaacTask = await res.json();
        onUpdate(task);
        if (task.status === 'done') { resolve(); return; }
        if (task.status === 'error') { reject(new Error(task.message)); return; }
        setTimeout(tick, intervalMs);
      } catch (e) {
        reject(e);
      }
    };
    setTimeout(tick, intervalMs);
  });
}

export function downloadGlb(blob: Blob, name = 'planlift_model.glb') {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}
