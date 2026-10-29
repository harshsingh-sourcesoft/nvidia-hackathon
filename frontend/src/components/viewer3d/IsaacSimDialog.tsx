'use client';
import { useState, useCallback, useEffect, useRef } from 'react';
import { startIsaacTask, pollIsaacTask, downloadGlb } from '@/utils/isaacSim';
import type { TaskStatus, IsaacTask } from '@/utils/isaacSim';

interface Props {
  onClose: () => void;
  exportGlb: () => Promise<Blob>;
  projectName: string;
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  starting:  'Starting…',
  checking:  'Checking Isaac Sim…',
  launching: 'Launching Isaac Sim…',
  waiting:   'Waiting for Isaac Sim to load (2–3 min)…',
  sending:   'Sending model to Isaac Sim…',
  done:      'Model opened in Isaac Sim!',
  error:     'Error',
};

const STEPS: TaskStatus[] = ['checking', 'launching', 'waiting', 'sending', 'done'];

function StepIndicator({ current }: { current: TaskStatus }) {
  return (
    <div className="flex items-center gap-1 mb-5">
      {STEPS.map((step, i) => {
        const idx     = STEPS.indexOf(current);
        const isDone  = i < idx || current === 'done';
        const isActive = step === current && current !== 'done';
        return (
          <div key={step} className="flex items-center gap-1">
            <div className={`w-2 h-2 rounded-full transition-all ${
              isDone   ? 'bg-green-400' :
              isActive ? 'bg-blue-400 animate-pulse' :
                         'bg-gray-600'
            }`} />
            {i < STEPS.length - 1 && (
              <div className={`h-px w-6 ${i < (STEPS.indexOf(current)) ? 'bg-green-400' : 'bg-gray-600'}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function IsaacSimDialog({ onClose, exportGlb, projectName }: Props) {
  const [phase, setPhase]     = useState<'idle' | 'running' | 'done' | 'error'>('idle');
  const [task, setTask]       = useState<IsaacTask>({ status: 'starting', message: '' });
  const [blobRef]             = useState<{ current: Blob | null }>({ current: null });
  const cancelRef             = useRef(false);

  // Auto-start as soon as the dialog opens
  useEffect(() => {
    cancelRef.current = false;
    run();
    return () => { cancelRef.current = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const run = useCallback(async () => {
    setPhase('running');
    try {
      // 1. Export the scene as GLB
      setTask({ status: 'starting', message: 'Exporting 3D scene…' });
      const blob = await exportGlb();
      blobRef.current = blob;
      if (cancelRef.current) return;

      // 2. Upload GLB + start background task on server
      const taskId = await startIsaacTask(blob);
      if (cancelRef.current) return;

      // 3. Poll until done or error
      await pollIsaacTask(
        taskId,
        (t) => { if (!cancelRef.current) setTask(t); },
      );

      if (!cancelRef.current) setPhase('done');
    } catch (e) {
      if (!cancelRef.current) {
        setPhase('error');
        setTask({ status: 'error', message: e instanceof Error ? e.message : String(e) });
      }
    }
  }, [exportGlb, blobRef]);

  const handleDownload = useCallback(async () => {
    try {
      const blob = blobRef.current ?? await exportGlb();
      blobRef.current = blob;
      downloadGlb(blob, `${projectName.replace(/\s+/g, '_')}.glb`);
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Export failed');
    }
  }, [blobRef, exportGlb, projectName]);

  const isRunning = phase === 'running';

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={isRunning ? undefined : onClose}
    >
      <div
        className="bg-[#1a1a2e] border border-[#2e2e4a] rounded-xl p-6 w-[440px] shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-white font-bold text-base flex items-center gap-2">
            <span className="text-xl">🤖</span> Open in Isaac Sim
          </h2>
          {!isRunning && (
            <button onClick={onClose} className="text-gray-500 hover:text-white text-lg leading-none">✕</button>
          )}
        </div>

        {/* Step progress dots */}
        {isRunning && <StepIndicator current={task.status} />}

        {/* Status message */}
        <div className={`flex items-center gap-3 px-3 py-3 rounded-lg mb-5 ${
          phase === 'done'  ? 'bg-green-900/40 border border-green-700/40' :
          phase === 'error' ? 'bg-red-900/40   border border-red-700/40'   :
                              'bg-[#0d0d1a]    border border-[#2e2e4a]'
        }`}>
          {isRunning && (
            <div className="w-4 h-4 border-2 border-blue-400 border-t-transparent rounded-full animate-spin flex-shrink-0" />
          )}
          {phase === 'done'  && <span className="text-green-400 text-lg flex-shrink-0">✓</span>}
          {phase === 'error' && <span className="text-red-400  text-lg flex-shrink-0">✗</span>}
          <div>
            <p className={`text-sm font-medium ${
              phase === 'done'  ? 'text-green-300' :
              phase === 'error' ? 'text-red-300'   :
                                  'text-gray-200'
            }`}>
              {task.status !== 'starting' ? STATUS_LABEL[task.status] : 'Exporting 3D scene…'}
            </p>
            {task.message && task.message !== STATUS_LABEL[task.status] && (
              <p className="text-xs text-gray-500 mt-0.5">{task.message}</p>
            )}
          </div>
        </div>

        {/* Setup instructions — shown only on "not found" error */}
        {phase === 'error' && task.message.includes('ISAAC_SIM_PATH') && (
          <div className="mb-4 p-3 bg-[#0d0d1a] rounded-lg border border-[#2e2e4a]">
            <p className="text-xs text-gray-400 font-semibold mb-2">One-time setup:</p>
            <ol className="text-xs text-gray-500 space-y-1.5 list-decimal list-inside">
              <li>
                Open <code className="text-blue-400 bg-blue-900/20 px-1 rounded">.env</code>{' '}
                next to <code className="text-blue-400 bg-blue-900/20 px-1 rounded">api_server.py</code>
              </li>
              <li>
                Add:{' '}
                <code className="text-green-400 bg-green-900/20 px-1 rounded">
                  ISAAC_SIM_PATH=C:\isaac-sim\isaac-sim.bat
                </code>
              </li>
              <li>Restart the API server and try again</li>
            </ol>
          </div>
        )}

        {/* Action buttons */}
        <div className="flex gap-2">
          {(phase === 'error') && (
            <button
              onClick={run}
              className="flex-1 px-4 py-2.5 rounded-lg text-sm font-semibold
                         bg-green-600 hover:bg-green-500 text-white transition-colors"
            >
              🔄 Retry
            </button>
          )}
          <button
            onClick={handleDownload}
            disabled={isRunning && !blobRef.current}
            className="flex-1 px-4 py-2.5 rounded-lg text-sm font-semibold transition-colors
                       bg-blue-600/80 hover:bg-blue-500 text-white
                       disabled:opacity-40 disabled:cursor-not-allowed"
          >
            ⬇ Download GLB
          </button>
          {(phase === 'done' || phase === 'error') && (
            <button
              onClick={onClose}
              className="flex-1 px-4 py-2.5 rounded-lg text-sm font-semibold
                         bg-gray-700 hover:bg-gray-600 text-white transition-colors"
            >
              Close
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
