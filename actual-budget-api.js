const actual = require('@actual-app/api');

const ACTUAL_SERVER_URL = process.env.ACTUAL_SERVER_URL || 'http://localhost:5006';
const ACTUAL_SERVER_PASSWORD = process.env.ACTUAL_SERVER_PASSWORD || 'changeme';
const DATA_DIR = process.env.DATA_DIR || './data';

// Redirect all console output to stderr so library logs don't corrupt the
// JSON response that Python reads from stdout.
console.log = (...args) => process.stderr.write(args.join(' ') + '\n');
console.info = console.log;
console.warn = console.log;
console.debug = console.log;

let isInitialized = false;
let currentBudgetId = null;

async function ensureInitialized(budgetSyncId) {
  if (!isInitialized) {
    await actual.init({
      dataDir: DATA_DIR,
      serverURL: ACTUAL_SERVER_URL,
      password: ACTUAL_SERVER_PASSWORD,
    });
    isInitialized = true;
  }

  if (currentBudgetId !== budgetSyncId) {
    await actual.downloadBudget(budgetSyncId);
    currentBudgetId = budgetSyncId;
  }
}

async function shutdown() {
  if (isInitialized) {
    await actual.shutdown();
    isInitialized = false;
    currentBudgetId = null;
  }
}

// ---------------------------------------------------------------------------
// CLI interface – reads JSON from stdin, writes JSON to stdout
// ---------------------------------------------------------------------------
async function main() {
  const raw = await new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => (data += chunk));
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });

  let request;
  try {
    request = JSON.parse(raw);
  } catch (err) {
    process.stdout.write(JSON.stringify({ error: 'Invalid JSON input: ' + err.message }) + '\n');
    process.exit(1);
  }

  const { action, budgetSyncId } = request;
  if (!action || !budgetSyncId) {
    process.stdout.write(JSON.stringify({ error: 'Missing "action" or "budgetSyncId"' }) + '\n');
    process.exit(1);
  }

  try {
    await ensureInitialized(budgetSyncId);

    let result;
    switch (action) {
      case 'getTransactions': {
        const { accountId, sinceDate, untilDate } = request;
        const transactions = await actual.getTransactions(accountId, sinceDate, untilDate);
        result = { data: transactions };
        break;
      }

      case 'updateTransaction': {
        const { transactionId, transaction } = request;
        await actual.updateTransaction(transactionId, transaction);
        result = { success: true };
        break;
      }

      case 'createSplit': {
        // updateTransaction cannot convert a simple transaction into a split.
        // The correct approach is to delete the original and re-create it via
        // addTransactions with a nested subtransactions array.
        const { accountId, transactionId, transaction } = request;
        await actual.deleteTransaction(transactionId);
        const ids = await actual.addTransactions(accountId, [transaction]);
        result = { success: true, ids };
        break;
      }

      case 'createTag': {
        const { tag } = request;
        const tagId = await actual.createTag({ tag });
        result = { success: true, tagId };
        break;
      }

      case 'getCategories': {
        const categories = await actual.getCategories();
        result = { data: categories };
        break;
      }

      case 'getTags': {
        const tags = await actual.getTags();
        result = { data: tags };
        break;
      }

      default:
        result = { error: 'Unknown action: ' + action };
    }

    process.stdout.write(JSON.stringify(result) + '\n');
  } catch (err) {
    process.stdout.write(JSON.stringify({ error: err.message }) + '\n');
    process.exit(1);
  } finally {
    await shutdown();
  }
}

main();
