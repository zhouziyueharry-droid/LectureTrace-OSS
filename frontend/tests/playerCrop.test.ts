import assert from 'node:assert/strict'
import { imageBounds, validViewCrop, viewSelection, viewTransform } from '../src/components/playerCrop.ts'

assert.deepEqual(viewSelection([0.5, 0.5], [1, 1], 16 / 9, 16 / 9), [0.5, 0.5, 0.5, 0.5])
assert.deepEqual(viewSelection([0.5, 0.5], [0, 0], 16 / 9, 16 / 9), [0, 0, 0.5, 0.5])
assert.deepEqual(imageBounds(1600, 900, 1920, 1080), { x: 0, y: 0, width: 1600, height: 900 })
assert.equal(viewTransform([0.5, 0, 0.5, 0.5], 1600, 900, 1920, 1080).transform, 'translate(-1600px, 0px) scale(2)')
assert.equal(validViewCrop([0.5, 0, 0.5, 0.5]), true)
assert.equal(validViewCrop([0.8, 0, 0.5, 0.5]), false)
