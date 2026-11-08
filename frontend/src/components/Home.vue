
import javax.swing.*;
import java.awt.*;

public class Main extends JFrame {

    public Main() {

        // ウィンドウの基本設定
        setTitle("プロジェクト管理アプリ");
        setSize(1220, 720);
        setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
        setLocationRelativeTo(null);

        // メインパネル
        JPanel mainPanel = new JPanel(new BorderLayout());
        mainPanel.setBackground(Color.WHITE);

        // =========================
        // 左側のメニュー
        // =========================

        JPanel leftPanel = new JPanel();
        leftPanel.setPreferredSize(new Dimension(380, 0));
        leftPanel.setBackground(Color.WHITE);
        leftPanel.setLayout(null);

        // 本日の日付ボタン
        JButton dateButton = new JButton("本日：○月○日○曜日");
        dateButton.setBounds(18, 13, 255, 33);
        leftPanel.add(dateButton);

        // その他ボタン
        JButton otherButton = new JButton("その他");
        otherButton.setBounds(273, 13, 86, 33);
        leftPanel.add(otherButton);

        // 期限が最も近いもの
        JButton deadlineButton = new JButton(
            "<html><center>" +
            "期限が最も近いもの<br>" +
            "○月○日期限<br>" +
            "プロジェクト：○○<br>" +
            "画面表示エラーの解決" +
            "</center></html>"
        );

        deadlineButton.setBounds(18, 45, 342, 143);
        leftPanel.add(deadlineButton);

        // 期限が近いプロジェクト
        JButton progressButton = new JButton(
            "期限が近いプロジェクトor進捗"
        );

        progressButton.setBounds(18, 201, 342, 144);
        leftPanel.add(progressButton);

        // プロジェクト一覧
        JButton projectListButton = new JButton("プロジェクト一覧");
        projectListButton.setBounds(18, 353, 342, 119);
        leftPanel.add(projectListButton);

        // エディタへ
        JButton editorButton = new JButton("エディタへ");
        editorButton.setBounds(26, 491, 244, 124);
        leftPanel.add(editorButton);

        // 設定
        JButton settingButton = new JButton("設定");
        settingButton.setBounds(25, 642, 87, 39);
        leftPanel.add(settingButton);

        // =========================
        // 中央部分
        // =========================

        JPanel centerPanel = new JPanel();
        centerPanel.setBackground(Color.WHITE);
        centerPanel.setLayout(null);

        JLabel homeLabel = new JLabel("ホーム");
        homeLabel.setFont(new Font("Serif", Font.PLAIN, 22));
        homeLabel.setBounds(55, 230, 150, 40);
        centerPanel.add(homeLabel);

        JLabel appNameLabel = new JLabel("アプリ名？");
        appNameLabel.setFont(new Font("Serif", Font.PLAIN, 22));
        appNameLabel.setBounds(120, 285, 200, 40);
        centerPanel.add(appNameLabel);

        // =========================
        // 右側のコミュニケーション欄
        // =========================

        CommunicationPanel communicationPanel =
            new CommunicationPanel();

        // 各パネルをメイン画面に配置
        mainPanel.add(leftPanel, BorderLayout.WEST);
        mainPanel.add(centerPanel, BorderLayout.CENTER);
        mainPanel.add(communicationPanel, BorderLayout.EAST);

        add(mainPanel);

        setVisible(true);
    }

    // 起動処理
    public static void main(String[] args) {
        SwingUtilities.invokeLater(() -> {
            new Main();
        });
    }
}


// =====================================
// コミュニケーション欄（独立クラス）
// =====================================

class CommunicationPanel extends JPanel {

    public CommunicationPanel() {

        setPreferredSize(new Dimension(400, 0));
        setBackground(Color.WHITE);
        setLayout(null);

        // 外側の青いパネル
        JPanel outerPanel = new JPanel(null);
        outerPanel.setBackground(new Color(24, 99, 128));
        outerPanel.setBounds(5, 113, 366, 393);

        // プロジェクト名ボタン
        JButton projectNameButton =
            new JButton("プロジェクト名：○○");

        projectNameButton.setBounds(25, 19, 320, 59);
        projectNameButton.setFont(
            new Font("Serif", Font.PLAIN, 22)
        );

        outerPanel.add(projectNameButton);

        // コミュニケーション欄
        SpeechBubblePanel communicationArea =
            new SpeechBubblePanel();

        communicationArea.setBounds(17, 97, 338, 195);

        outerPanel.add(communicationArea);

        // 最終変更情報ボタン
        JButton updateButton = new JButton(
            "<html><center>" +
            "最終変更：○○時間前<br>" +
            "変更者：○○" +
            "</center></html>"
        );

        updateButton.setBounds(24, 297, 331, 66);
        updateButton.setFont(
            new Font("Serif", Font.PLAIN, 20)
        );

        outerPanel.add(updateButton);

        add(outerPanel);
    }
}


// =====================================
// 吹き出し型コミュニケーション欄
// =====================================

class SpeechBubblePanel extends JPanel {

    public SpeechBubblePanel() {
        setOpaque(false);
        setLayout(new GridBagLayout());
    }

    @Override
    protected void paintComponent(Graphics g) {

        super.paintComponent(g);

        Graphics2D g2 = (Graphics2D) g.create();

        g2.setRenderingHint(
            RenderingHints.KEY_ANTIALIASING,
            RenderingHints.VALUE_ANTIALIAS_ON
        );

        // 吹き出しの白い本体
        g2.setColor(Color.WHITE);

        g2.fillRoundRect(
            1, 1,
            getWidth() - 2,
            getHeight() - 24,
            30, 30
        );

        // 吹き出しの枠線
        g2.setColor(Color.BLACK);
        g2.setStroke(new BasicStroke(2));

        g2.drawRoundRect(
            1, 1,
            getWidth() - 2,
            getHeight() - 24,
            30, 30
        );

        // 吹き出しの三角部分
        int center = getWidth() / 2;

        int[] xPoints = {
            center - 42,
            center,
            center + 42
        };

        int[] yPoints = {
            getHeight() - 25,
            getHeight() - 1,
            getHeight() - 25
        };

        g2.setColor(Color.WHITE);
        g2.fillPolygon(xPoints, yPoints, 3);

        g2.setColor(Color.BLACK);
        g2.drawLine(
            center - 42,
            getHeight() - 25,
            center,
            getHeight() - 1
        );

        g2.drawLine(
            center,
            getHeight() - 1,
            center + 42,
            getHeight() - 25
        );

        g2.dispose();
    }

    @Override
    public void addNotify() {
        super.addNotify();

        JLabel label = new JLabel("コミュニケーション欄");
        label.setFont(new Font("Serif", Font.PLAIN, 21));

        add(label);
    }
}