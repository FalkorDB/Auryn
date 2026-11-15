<?php

namespace App\Models;

class Logger
{
    private array $messages = [];

    public function log(string $message): void
    {
        $this->messages[] = $message;
    }
}
