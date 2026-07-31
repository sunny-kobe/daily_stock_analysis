import { describe, expect, it } from 'vitest';
import { instructionToAxes, selectableInstructionFromAxes } from '../portfolioInstruction';

describe('instructionToAxes', () => {
  it('derives a selectable instruction from the preserved internal axes', () => {
    expect(selectableInstructionFromAxes('hold', 'add_in_batches')).toBe('add');
    expect(selectableInstructionFromAxes('hold', 'wait')).toBe('hold');
    expect(selectableInstructionFromAxes('reduce', 'no_add')).toBe('reduce');
    expect(selectableInstructionFromAxes('exit', 'no_add')).toBe('exit');
  });

  it('maps simple actions to the required internal axes', () => {
    expect(instructionToAxes('add', 'hold', 'wait')).toEqual({
      positionAction: 'hold',
      incrementalAction: 'add_in_batches',
    });
    expect(instructionToAxes('reduce', 'hold', 'wait')).toEqual({
      positionAction: 'reduce',
      incrementalAction: 'no_add',
    });
    expect(instructionToAxes('exit', 'hold', 'wait')).toEqual({
      positionAction: 'exit',
      incrementalAction: 'no_add',
    });
  });

  it('preserves the existing wait or no-add detail when hold is unchanged', () => {
    expect(instructionToAxes('hold', 'hold', 'wait')).toEqual({
      positionAction: 'hold',
      incrementalAction: 'wait',
    });
    expect(instructionToAxes('hold', 'hold', 'no_add')).toEqual({
      positionAction: 'hold',
      incrementalAction: 'no_add',
    });
  });

  it('uses no-add when switching from a reducing action to hold', () => {
    expect(instructionToAxes('hold', 'reduce', 'no_add')).toEqual({
      positionAction: 'hold',
      incrementalAction: 'no_add',
    });
  });

  it('rejects insufficient as a human-selected action', () => {
    expect(() => instructionToAxes('insufficient', 'hold', 'wait')).toThrow(
      'insufficient_instruction_not_selectable',
    );
  });
});
