// Copyright 2014-2021 Scalyr Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

const core = require('@actions/core');
const cache = require('@actions/cache');
const path = require('path')

async function executeRunner() {
    const stepsIdsJSON = core.getInput("steps_ids");
    const lookupOnlyStr = core.getInput("lookup_only");
    const lookupOnly = lookupOnlyStr === 'true' ? true : false;
    const cacheRoot = core.getInput("cache_root");


    const stepsIDs = JSON.parse(stepsIdsJSON);
    const cacheVersionSuffix = core.getInput("cache_version_suffix");

    const missingCaches = []

    for (let stepID of stepsIDs) {
        const cachePath = path.join(cacheRoot, stepID);
        const finalCacheKey = `${stepID}_${cacheVersionSuffix}`
        const result = await cache.restoreCache(
            paths=[cachePath],
            primaryKey=finalCacheKey,
            restoreKeys=[],
            options={ lookupOnly: lookupOnly }
        )
        if (typeof result !== "undefined") {
            console.log(`Cache for the step with key ${finalCacheKey} is found.`)
        } else {
            console.log(`Cache for the step with key ${finalCacheKey} is not found.`)
            missingCaches.push(stepID)
        }
    }

    core.setOutput("missing_steps_ids_json", JSON.stringify(missingCaches));
}


async function run() {
    // Entry function. Just catch any error and pass it to GH Actions.
  try {
      await executeRunner()
  } catch (error) {
    core.setFailed(error.message);
  }
}

run()


