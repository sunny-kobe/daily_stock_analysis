export type HoldingInstruction = 'add' | 'hold' | 'reduce' | 'exit' | 'insufficient';
export type SelectableHoldingInstruction = Exclude<HoldingInstruction, 'insufficient'>;

export type PositionAction = 'hold' | 'reduce' | 'exit';
export type IncrementalAction = 'add_in_batches' | 'wait' | 'no_add';

type InternalAxes = {
  positionAction: PositionAction;
  incrementalAction: IncrementalAction;
};

export function selectableInstructionFromAxes(
  positionAction: string,
  incrementalAction: string,
): SelectableHoldingInstruction {
  if (positionAction === 'reduce') return 'reduce';
  if (positionAction === 'exit') return 'exit';
  if (incrementalAction === 'add_in_batches') return 'add';
  return 'hold';
}

export function instructionToAxes(
  instruction: HoldingInstruction,
  currentPositionAction: string,
  currentIncrementalAction: string,
): InternalAxes {
  if (instruction === 'insufficient') {
    throw new Error('insufficient_instruction_not_selectable');
  }
  if (instruction === 'add') {
    return { positionAction: 'hold', incrementalAction: 'add_in_batches' };
  }
  if (instruction === 'reduce') {
    return { positionAction: 'reduce', incrementalAction: 'no_add' };
  }
  if (instruction === 'exit') {
    return { positionAction: 'exit', incrementalAction: 'no_add' };
  }
  if (
    currentPositionAction === 'hold'
    && (currentIncrementalAction === 'wait' || currentIncrementalAction === 'no_add')
  ) {
    return {
      positionAction: 'hold',
      incrementalAction: currentIncrementalAction,
    };
  }
  return { positionAction: 'hold', incrementalAction: 'no_add' };
}
