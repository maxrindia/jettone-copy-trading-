import bcrypt from "bcryptjs";
import readline from "node:readline";

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

function promptHidden(question) {
  return new Promise((resolve) => {
    const stdin = process.stdin;
    process.stderr.write(question);
    let password = "";
    if (stdin.isTTY) stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding("utf8");
    const onData = (char) => {
      if (char === "\r" || char === "\n") {
        stdin.removeListener("data", onData);
        if (stdin.isTTY) stdin.setRawMode(false);
        stdin.pause();
        process.stderr.write("\n");
        resolve(password);
      } else if (char === "\u0003") {
        process.exit(1);
      } else {
        password += char;
      }
    };
    stdin.on("data", onData);
  });
}

rl.question("Admin email: ", async (email) => {
  const password = await promptHidden("Password (input hidden): ");
  if (!email.includes("@") || password.length < 12) {
    console.error("Email required; password must be at least 12 characters.");
    process.exit(1);
  }
  const hash = await bcrypt.hash(password, 12);
  console.log(`\nAdd these to your .env:\n`);
  console.log(`ADMIN_EMAIL=${email.trim().toLowerCase()}`);
  console.log(`ADMIN_PASSWORD_HASH=${hash}`);
  rl.close();
});
